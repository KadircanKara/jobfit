"""The fit gate, run through `claude -p`.

Stage 2 needs a model. A background worker has no Claude Code session, so it
shells out to the CLI in print mode. The subprocess is given no tools at all:
it can answer and nothing else, so a scoring pass can never touch the corpus,
the filesystem, or the network.

A batch that comes back unreadable is retried once and then left alone. Ungated
jobs keep their place in the database and get scored on the next run, which is
a better outcome than inventing a score for them.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from jobhunt.config import Config
from jobhunt.web import agent

DEFAULT_TIMEOUT = 300.0
ARRAY = re.compile(r"\[.*\]", re.S)

Runner = Callable[[list[str], str, float], str]


def _subprocess_runner(argv: list[str], prompt: str, timeout: float) -> str:
    done = subprocess.run(
        argv, input=prompt, capture_output=True, text=True, timeout=timeout, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"claude exited {done.returncode}")
    return done.stdout


class Gate:
    def __init__(
        self,
        *,
        config: Config | None = None,
        runner: Runner | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.config = config
        self.runner = runner or _subprocess_runner
        self.timeout = timeout

    def argv(self) -> list[str]:
        # --allowedTools "" is the safety property, not a nicety: the gate is
        # only ever asked for a judgement, so it gets no way to act on one.
        if self.config is not None:
            return agent.argv_for(self.config, "gate", tools="")
        return [shutil.which("claude") or "claude", "-p", "--output-format", "json",
                "--allowedTools", ""]

    def score(self, *, prompt: str, batch_size: int) -> list[dict[str, Any]]:
        """Return validated verdicts, or an empty list if the batch is unusable."""
        for _attempt in (1, 2):
            try:
                raw = self.runner(self.argv(), prompt, self.timeout)
            except Exception:
                continue
            verdicts = _parse(raw)
            if verdicts:
                return _valid(verdicts)
        return []


def _parse(raw: str) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        return []
    text = raw.strip()

    # `--output-format json` wraps the answer; older shapes hand it back bare.
    try:
        envelope = json.loads(text)
        if isinstance(envelope, list):
            return envelope
        if isinstance(envelope, dict) and "result" in envelope:
            text = str(envelope["result"])
    except json.JSONDecodeError:
        pass

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Prose around the array is common enough to handle rather than reject.
    match = ARRAY.search(text)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _valid(verdicts: list[Any]) -> list[dict[str, Any]]:
    """Keep only verdicts that name a job and score it inside the scale."""
    out: list[dict[str, Any]] = []
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        if "job_id" not in verdict:
            continue
        try:
            job_id = int(verdict["job_id"])
            score = float(verdict.get("score"))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= score <= 1.0:
            continue
        out.append(
            {
                "job_id": job_id,
                "score": score,
                "reasoning": str(verdict.get("reasoning", "")).strip(),
                "red_flags": list(verdict.get("red_flags") or []),
            }
        )
    return out
