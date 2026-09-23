"""Which model runs which phase, and how hard it thinks.

Every model call in this project shells out to `claude -p`. Until now none of
them named a model or an effort level, so all four phases silently inherited
whatever the CLI session happened to default to — one setting for scoring a
batch of jobs, rewriting a CV, reviewing it for fabrication, and answering a
question about a posting.

Those are not the same task, and they should not be forced onto the same
model. This module is the one place that builds the argv, so a phase can be
moved to a cheaper model without touching the code that uses it.

Unset means inherit. A project that names nothing behaves exactly as it did
before this module existed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from jobhunt.config import Config

# The phases that cost a model call. Sync and deterministic ranking are not
# here because they never call one: sync is HTTP, ranking is Python and a
# filters file. "upwork" is the exception to "sync is HTTP" - its fetch has no
# HTTP client of its own, only the Upwork MCP reached through this same `claude
# -p` mechanism, so its effort needs the same startup validation as the rest.
PHASES = ("gate", "tailor", "review", "revise", "upwork", "import", "template")

# What `claude --effort` accepts. Naming one this project does not know would be
# passed straight to the CLI and fail every call in that phase.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_TIMEOUT = 1800.0


class AgentError(RuntimeError):
    """A model call that failed, phrased for the person who triggered it."""


def flags(config: Config, phase: str) -> list[str]:
    """`--model` and `--effort` for this phase, or nothing to inherit both."""
    argv: list[str] = []
    model = config.get("models", phase, "model")
    if model:
        argv += ["--model", str(model)]
    effort = config.get("models", phase, "effort")
    if effort and str(effort).lower() in EFFORTS:
        argv += ["--effort", str(effort).lower()]
    return argv


def check(config: Config) -> None:
    """Refuse to start on a phase configured with an effort the CLI rejects.

    Called once when the app is built. A typo here would otherwise surface as
    every call in that phase failing, one confusing run at a time.
    """
    bad = []
    for phase in PHASES:
        effort = config.get("models", phase, "effort")
        if effort and str(effort).lower() not in EFFORTS:
            bad.append(f"models.{phase}.effort = {effort!r}")
    if bad:
        raise ValueError(
            "unknown effort level in config.yaml: "
            + "; ".join(bad)
            + f". pick one of {', '.join(EFFORTS)}, or leave it unset to inherit."
        )


def argv_for(config: Config, phase: str, *, tools: str) -> list[str]:
    binary = shutil.which("claude") or "claude"
    return [
        binary, "-p", "--output-format", "json",
        "--allowedTools", tools,
        *flags(config, phase),
    ]


def run(
    config: Config,
    phase: str,
    prompt: str,
    *,
    tools: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """One model call for one phase. Returns the answer text, not the envelope."""
    done = subprocess.run(
        argv_for(config, phase, tools=tools),
        input=prompt, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if done.returncode != 0:
        raise AgentError(done.stderr.strip() or f"claude exited {done.returncode}")
    return text_of(done.stdout)


class NotJson(ValueError):
    """An answer with no JSON object in it, or one that does not parse."""

    def __init__(self, found: bool) -> None:
        super().__init__("unreadable JSON" if found else "no JSON object")
        self.found = found


def json_object(raw: str) -> Any:
    """The JSON object in a model's answer. Models wrap it in prose often
    enough that "from the first brace to the last" is the reliable reading."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise NotJson(found=False)
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise NotJson(found=True) from exc


def text_of(raw: str) -> str:
    """The answer inside a `--output-format json` envelope, or the raw output."""
    try:
        envelope: Any = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(envelope, dict) and "result" in envelope:
        return str(envelope["result"])
    return raw
