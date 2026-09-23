"""The tailoring skill's own ATS check, run on a template preview.

A template can look right and still extract badly: ligatures that match no
search, headings that do not come out as their own line, text in two columns.
The skill already knows how to find those, so a new template is put through
the same script before it is accepted. The findings are warnings, not a gate:
the person chooses the look, and this tells them what it costs.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from jobhunt.config import Config

DEFAULT_SCRIPT = pathlib.Path.home() / ".claude/skills/tailoring-cv/scripts/ats_check.py"
TIMEOUT = 120.0
_FINDING = re.compile(r"^\[(FAIL|WARN)\]\s*(.+)$")

Runner = Callable[[list[str], pathlib.Path], tuple[int, str]]


@dataclasses.dataclass(frozen=True)
class Report:
    ran: bool
    failures: list[str]
    warnings: list[str]
    note: str = ""


def check(config: Config, tex: str, pdf: bytes, *, runner: Runner | None = None) -> Report:
    script = pathlib.Path(str(config.get("tailoring", "ats_check") or DEFAULT_SCRIPT)).expanduser()
    python = shutil.which("python3")
    if not script.exists() or python is None:
        return Report(False, [], [], f"the ATS check was skipped: {script} was not found")
    with tempfile.TemporaryDirectory(prefix="jobhunt-ats-") as raw:
        folder = pathlib.Path(raw)
        (folder / "cv.tex").write_text(tex, encoding="utf-8")
        (folder / "cv.pdf").write_bytes(pdf)
        _, output = (runner or _run)([python, str(script), str(folder / "cv.pdf")], folder)
    found = _findings(output)
    return Report(
        True,
        [text for level, text in found if level == "FAIL"],
        [text for level, text in found if level == "WARN"],
    )


def _findings(output: str) -> list[tuple[str, str]]:
    """Each [FAIL] or [WARN] line with the indented lines under it, which is
    where the script names the words, links or glyphs it is talking about."""
    found: list[tuple[str, list[str]]] = []
    for line in output.splitlines():
        match = _FINDING.match(line)
        if match:
            found.append((match.group(1), [match.group(2).strip()]))
        elif found and line.startswith((" ", "\t")) and line.strip() and not line.lstrip().startswith("["):
            found[-1][1].append(line.strip())
        elif not line.startswith((" ", "\t")):
            found.append(("", []))
    return [(level, " ".join(parts)) for level, parts in found if level]


def _run(argv: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "[WARN] the ATS check did not finish"
    return done.returncode, done.stdout + done.stderr
