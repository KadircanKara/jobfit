"""Batch CV tailoring, gated by the reviewer.

The loop mirrors what the tailoring-cv skill does by hand: prepare the folder,
cut the CV, put it in front of a reviewer with fresh context, and send findings
back until it approves or the rounds run out.

Two rules are not negotiable. A CV the reviewer never approved is marked
cv_failed and kept, never shipped as ready. And findings are kept even when the
run ends in approval, because a caught fabrication is the most useful thing
this loop produces and the person applying needs to see it.

This module never contacts an employer. It writes files into a folder.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import threading
from typing import Any, Protocol

from jobhunt.config import Config
from jobhunt.web.events import EventLog

MAX_ROUNDS = 3
CONCURRENCY = 2
STEP_TIMEOUT = 1800.0

UNATTENDED = "unattended run, reviewer agent gates"


class TailorError(RuntimeError):
    """A job that cannot be tailored, phrased for the person who picked it."""


@dataclasses.dataclass
class JobRun:
    job_id: int
    state: str = "queued"  # queued | running | approved | failed | cancelled
    rounds: int = 0
    folder: str | None = None
    fit: float | None = None
    findings: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Steps(Protocol):
    def prepare(self, job_id: int) -> str: ...
    def tailor(self, folder: str, findings: list[str]) -> str: ...
    def review(self, folder: str, verifier: str) -> dict[str, Any]: ...
    def mark(self, job_id: int, status: str) -> None: ...


class TailorBatch:
    def __init__(
        self,
        *,
        job_ids: list[int],
        steps: Steps,
        log: EventLog,
        max_rounds: int = MAX_ROUNDS,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self.rows = [JobRun(job_id=job_id) for job_id in job_ids]
        self.steps = steps
        self.log = log
        self.max_rounds = max_rounds
        self.concurrency = max(1, concurrency)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- control ---------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def running(self) -> bool:
        return any(row.state in ("queued", "running") for row in self.rows) and (
            self._thread is not None and self._thread.is_alive()
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="jobhunt-tailor")
        self._thread.start()

    def state(self) -> list[dict[str, Any]]:
        return [row.as_dict() for row in self.rows]

    # --- the loop --------------------------------------------------------

    def run(self) -> list[JobRun]:
        queue = list(self.rows)
        threads: list[threading.Thread] = []
        lock = threading.Semaphore(self.concurrency)

        def work(row: JobRun) -> None:
            with lock:
                self._one(row)

        for row in queue:
            thread = threading.Thread(target=work, args=(row,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return self.rows

    def _one(self, row: JobRun) -> None:
        if self.stopping:
            row.state = "cancelled"
            return
        row.state = "running"
        self._say(row, "reading the posting")

        try:
            row.folder = self.steps.prepare(row.job_id)
        except Exception as exc:
            # An incomplete posting is the right place to stop: tailoring
            # against a truncated JD produces a CV aimed at the wrong job.
            row.state = "failed"
            row.error = str(exc)
            self._say(row, f"skipped · {exc}", level="warning")
            return

        while row.rounds < self.max_rounds:
            if self.stopping:
                row.state = "cancelled"
                return
            row.rounds += 1
            self._say(row, f"cutting the CV · round {row.rounds}")
            try:
                verifier = self.steps.tailor(row.folder, row.findings)
                verdict = self.steps.review(row.folder, verifier)
            except Exception as exc:
                row.state = "failed"
                row.error = str(exc)
                self._say(row, f"failed · {exc}", level="error")
                self.steps.mark(row.job_id, "cv_failed")
                return

            findings = _findings_of(verdict)
            for finding in findings:
                if finding not in row.findings:
                    row.findings.append(finding)
            row.fit = _fit_of(verdict)

            if str(verdict.get("verdict", "")).lower() == "approve":
                row.state = "approved"
                self.steps.mark(row.job_id, "cv_ready")
                self._say(row, f"approved after {row.rounds} round(s) · fit {row.fit}")
                return
            self._say(row, f"reviewer asked for changes · {'; '.join(findings) or 'see findings'}",
                      level="warning")

        row.state = "failed"
        self.steps.mark(row.job_id, "cv_failed")
        self._say(
            row,
            f"not approved after {self.max_rounds} rounds · folder kept for you to finish",
            level="warning",
        )

    def _say(self, row: JobRun, message: str, level: str = "info") -> None:
        self.log.emit(phase="tailor", message=f"job {row.job_id}: {message}", level=level)


def _findings_of(verdict: dict[str, Any]) -> list[str]:
    fabrication = verdict.get("fabrication") or {}
    return [str(item) for item in (fabrication.get("findings") or [])]


def _fit_of(verdict: dict[str, Any]) -> float | None:
    fit = verdict.get("fit") or {}
    try:
        return float(fit.get("score"))
    except (TypeError, ValueError):
        return None


# --- the real steps ---------------------------------------------------------


class ClaudeSteps:
    """The real thing: the CLI for the folder, `claude -p` for the two agents."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = pathlib.Path(str(config.get("tailoring", "applications_root"))).expanduser()
        self.master = pathlib.Path(str(config.get("tailoring", "master_tex"))).expanduser()

    def prepare(self, job_id: int) -> str:
        from jobhunt import applications
        from jobhunt.db.session import session_scope

        with session_scope(self.config.db_path) as session:
            application = applications.apply(session, job_id, tailor=False)
            folder = getattr(application, "folder", None)
        if not folder:
            raise TailorError(f"job {job_id} has no complete posting to tailor against")
        return str(folder)

    def tailor(self, folder: str, findings: list[str]) -> str:
        notes = ""
        if findings:
            notes = (
                "\n\nThe reviewer rejected the previous cut. Fix these at source, "
                "do not paper over them:\n- " + "\n- ".join(findings)
            )
        prompt = (
            f"Use the tailoring-cv skill for the job in this folder:\n{folder}\n\n"
            f"Master CV: {self.master}\n\n"
            f"This is an {UNATTENDED}. Skip the chat approval step.\n"
            f"Return the verifier output verbatim.{notes}"
        )
        return _claude(prompt, tools="Read Write Edit Bash Glob Grep")

    def review(self, folder: str, verifier: str) -> dict[str, Any]:
        prompt = (
            "You are the cv-jd-reviewer. Review this tailored CV against the posting "
            "and the master, and return only the JSON verdict object.\n\n"
            f"Folder: {folder}\nMaster: {self.master}\n\nVerifier output:\n{verifier}"
        )
        raw = _claude(prompt, tools="Read Glob Grep")
        return _json_object(raw)

    def mark(self, job_id: int, status: str) -> None:
        from jobhunt.render import csv_export

        csv_export.set_cv_status(csv_export.csv_path(self.config), job_id, status)


def _claude(prompt: str, *, tools: str) -> str:
    binary = shutil.which("claude") or "claude"
    done = subprocess.run(
        [binary, "-p", "--output-format", "json", "--allowedTools", tools],
        input=prompt, capture_output=True, text=True, timeout=STEP_TIMEOUT, check=False,
    )
    if done.returncode != 0:
        raise TailorError(done.stderr.strip() or f"claude exited {done.returncode}")
    try:
        envelope = json.loads(done.stdout)
        return str(envelope.get("result", done.stdout))
    except json.JSONDecodeError:
        return done.stdout


def _json_object(raw: str) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise TailorError("the reviewer did not return a verdict")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TailorError("the reviewer's verdict was not readable") from exc
