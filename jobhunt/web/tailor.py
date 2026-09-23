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
import pathlib
import shlex
import sys
import threading
from typing import Any, Protocol

from jobhunt.config import Config
from jobhunt.cv import latex, tailored, templates
from jobhunt.cv import store as cvstore
from jobhunt.web import agent
from jobhunt.web.events import EventLog

MAX_ROUNDS = 3
CONCURRENCY = 2
STEP_TIMEOUT = 1800.0

# A tailored CV ships at two pages. Enforced here, while the loop still has
# rounds to spend, rather than left for the person to discover in the studio.
MAX_PAGES = 2

UNATTENDED = "unattended run, reviewer agent gates"


class TailorError(RuntimeError):
    """A job that cannot be tailored, phrased for the person who picked it."""


class TemplateChoice(ValueError):
    """A template pick that cannot start a batch, with the field it concerns."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclasses.dataclass
class JobRun:
    job_id: int
    state: str = "queued"  # queued | running | approved | failed | cancelled
    rounds: int = 0
    folder: str | None = None
    fit: float | None = None
    pages: int | None = None
    findings: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    # Filled in by whoever starts the batch, so the browser has something to
    # call each row besides its id, and a way back to the posting itself.
    title: str = ""
    company: str = ""
    url: str | None = None
    # The template this job's master is rendered in. None means no profile
    # existed when the batch started, so the job is cut from the global master
    # exactly as it was before templates.
    template_id: str | None = None
    template_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Steps(Protocol):
    def prepare(self, job_id: int, template_id: str | None) -> str: ...
    def tailor(self, folder: str, findings: list[str]) -> str: ...
    def delivered(self, folder: str) -> bool: ...
    def review(self, folder: str, verifier: str) -> dict[str, Any]: ...
    def mark(self, job_id: int, status: str) -> None: ...
    def pages(self, folder: str) -> int | None: ...


class TailorBatch:
    def __init__(
        self,
        *,
        job_ids: list[int],
        steps: Steps,
        log: EventLog,
        max_rounds: int = MAX_ROUNDS,
        concurrency: int = CONCURRENCY,
        max_pages: int = MAX_PAGES,
        templates: dict[int, str] | None = None,
    ) -> None:
        chosen = templates or {}
        self.rows = [JobRun(job_id=job_id, template_id=chosen.get(job_id)) for job_id in job_ids]
        self.steps = steps
        self.log = log
        self.max_rounds = max_rounds
        self.max_pages = max_pages
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
            row.folder = self.steps.prepare(row.job_id, row.template_id)
        except Exception as exc:
            # An incomplete posting is the right place to stop: tailoring
            # against a truncated JD produces a CV aimed at the wrong job. So is
            # a template that cannot build the profile.
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
                if not self.steps.delivered(row.folder):
                    # The skill stops before the deliverable on a check it
                    # cannot fix by cutting (an ATS parse failure is the usual
                    # one, and it lives in the template). Another round would
                    # stop in the same place, and an approval would ship nothing.
                    self._stopped(row, verifier)
                    return
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
                row.pages = self._pages(row)
                if self._runs_long(row) and row.rounds < self.max_rounds:
                    # Length is handled the same way a reviewer objection is:
                    # as a finding the next cut has to answer.
                    note = f"the CV runs to {row.pages} pages and must fit {self.max_pages}"
                    if note not in row.findings:
                        row.findings.append(note)
                    self._say(
                        row,
                        f"approved on content but {row.pages} pages · cutting it to "
                        f"{self.max_pages}",
                        level="warning",
                    )
                    continue
                row.state = "approved"
                self.steps.mark(row.job_id, "cv_ready")
                length = f" · {row.pages} pages" if row.pages else ""
                self._say(row, f"approved after {row.rounds} round(s) · fit {row.fit}{length}")
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

    def _stopped(self, row: JobRun, verifier: str) -> None:
        failed = [line.strip() for line in verifier.splitlines() if "[FAIL]" in line]
        row.state = "failed"
        row.error = "the tailoring run stopped before it wrote the CV" + (
            f" · {'; '.join(failed[:3])}" if failed else ""
        )
        self.steps.mark(row.job_id, "cv_failed")
        self._say(row, row.error, level="warning")

    def _runs_long(self, row: JobRun) -> bool:
        return bool(row.pages and row.pages > self.max_pages)

    def _pages(self, row: JobRun) -> int | None:
        """Pages in the compiled CV. A toolchain that cannot say is not a
        reason to hold up a CV the reviewer already approved."""
        try:
            return self.steps.pages(row.folder or "")
        except Exception as exc:
            self._say(row, f"could not measure the page count · {exc}", level="warning")
            return None

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

    def __init__(self, config: Config, runner: latex.Runner | None = None) -> None:
        self.config = config
        self.runner = runner
        # One master per template for the whole batch: every job that shares a
        # template is cut from the same build of the same profile. A template
        # that failed is remembered too, so it fails each of its jobs at once.
        self._snapshots: dict[str, tailored.Snapshot | TailorError] = {}
        self._snapshotting = threading.Lock()

    def prepare(self, job_id: int, template_id: str | None) -> str:
        from jobhunt import applications
        from jobhunt.db.session import session_scope

        # Before the folder: a template that cannot build touches nothing.
        snap = self._snapshot(template_id) if template_id else None
        with session_scope(self.config.db_path) as session:
            application = applications.apply(
                self.config, session, job_id, tailor=False, status="tailored"
            )
            folder = getattr(application, "folder", None)
        if not folder:
            raise TailorError(f"job {job_id} has no complete posting to tailor against")
        if snap is not None:
            tailored.write(folder, snap)
        return str(folder)

    def _snapshot(self, template_id: str) -> tailored.Snapshot:
        with self._snapshotting:
            if template_id not in self._snapshots:
                try:
                    made: tailored.Snapshot | TailorError = tailored.snapshot(
                        self.config, template_id, runner=self.runner
                    )
                except tailored.TailoredError as exc:
                    made = TailorError(str(exc))
                self._snapshots[template_id] = made
            found = self._snapshots[template_id]
        if isinstance(found, TailorError):
            raise found
        return found

    def tailor(self, folder: str, findings: list[str]) -> str:
        master = tailored.master_for(self.config, folder)
        notes = ""
        if findings:
            notes = (
                "\n\nThe reviewer rejected the previous cut. Fix these at source, "
                "do not paper over them:\n- " + "\n- ".join(findings)
            )
        prompt = (
            f"Use the tailoring-cv skill for the job in this folder:\n{folder}\n\n"
            f"Master CV: {master}\n"
            "That file is the master for this run: read it wherever the skill says master.tex, "
            "give it to verify_cv.py with --master, and never edit it.\n\n"
            "Compile with this command, run in the folder, in place of the lualatex line. It "
            f"leaves cv.pdf and cv.log there as lualatex would:\n{self.compile_command()}\n\n"
            f"This is an {UNATTENDED}. Skip the chat approval step.\n"
            f"Return the verifier output verbatim.{notes}"
        )
        return agent.run(
            self.config, "tailor", prompt,
            tools="Read Write Edit Bash Glob Grep", timeout=STEP_TIMEOUT,
        )

    def compile_command(self) -> str:
        """How the agent builds cv.tex: the app's own build, sandboxed, in the
        template's engine. The agent's lualatex would run an uploaded preamble
        with full access."""
        argv = [sys.executable, "-m", "jobhunt.cv.tailored", "--config", str(self.config.path), "cv.tex"]
        return " ".join(shlex.quote(part) for part in argv)

    def review(self, folder: str, verifier: str) -> dict[str, Any]:
        master = tailored.master_for(self.config, folder)
        prompt = (
            "You are the cv-jd-reviewer. Review this tailored CV against the posting "
            "and the master, and return only the JSON verdict object.\n\n"
            f"Folder: {folder}\nMaster: {master}\n\nVerifier output:\n{verifier}"
        )
        raw = agent.run(
            self.config, "review", prompt, tools="Read Glob Grep", timeout=STEP_TIMEOUT
        )
        return _json_object(raw)

    def delivered(self, folder: str) -> bool:
        """Whether the CV named for sending is the one this round built. The
        skill copies cv.pdf to it last, so a round that stopped early leaves
        either no named PDF or the previous round's."""
        from jobhunt import applications

        named = applications.tailored_cv(folder)
        built = pathlib.Path(folder) / "cv.pdf"
        return named is not None and built.is_file() and named.read_bytes() == built.read_bytes()

    def mark(self, job_id: int, status: str) -> None:
        from jobhunt.render import csv_export

        csv_export.set_cv_status(csv_export.csv_path(self.config), job_id, status)

    def pages(self, folder: str) -> int | None:
        from jobhunt.web import revise as revise_module

        tex = pathlib.Path(folder) / revise_module.TEX_NAME
        if not tex.exists():
            return None
        built = tailored.build(self.config, tex, runner=self.runner)
        return built.pages if built.ok else None


def choose_templates(
    config: Config, job_ids: list[int], requested: Any
) -> dict[int, templates.Template]:
    """The template each job's master is rendered in, checked before a batch starts.

    With no saved profile there is nothing to render, so no job gets a template
    and every one is cut from the global master, as before templates existed.
    Otherwise a job the request does not name takes the default template.
    """
    if requested is not None and not isinstance(requested, dict):
        raise TemplateChoice("templates", "templates must map a job id to a template id")
    requested = requested or {}
    try:
        saved = cvstore.read(config)
    except cvstore.StoreError as exc:
        raise TemplateChoice("profile", str(exc)) from exc
    if saved is None:
        if requested:
            raise TemplateChoice("profile", "save a profile before choosing a template for a job")
        return {}

    by_job: dict[int, str] = {}
    for key, value in requested.items():
        try:
            job_id = int(key)
        except (TypeError, ValueError):
            raise TemplateChoice("templates", f"{key!r} is not a job id") from None
        if job_id not in job_ids:
            raise TemplateChoice("templates", f"job {job_id} is not in this batch")
        if not isinstance(value, str):
            raise TemplateChoice("templates", f"the template for job {job_id} must be a template id")
        by_job[job_id] = value

    default = templates.default_id(config)
    chosen: dict[int, templates.Template] = {}
    for job_id in job_ids:
        try:
            chosen[job_id] = templates.get(config, by_job.get(job_id, default))
        except templates.TemplateError as exc:
            raise TemplateChoice("templates", str(exc)) from exc
    return chosen


def _json_object(raw: str) -> dict[str, Any]:
    try:
        return agent.json_object(raw)
    except agent.NotJson as exc:
        if exc.found:
            raise TailorError("the reviewer's verdict was not readable") from exc
        raise TailorError("the reviewer did not return a verdict") from exc
