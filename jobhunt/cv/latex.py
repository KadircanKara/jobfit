"""Compiling a rendered CV.

One place builds LaTeX for the CV builder, so every PDF comes out of the same
engine with the same flags. lualatex is the default because it is the engine
the tailoring skill compiles with: a master built any other way would not be
the document the tailored CVs are cut from and compared against.

`-no-shell-escape` is passed every time rather than left to the TeX
installation's default. Templates can be uploaded, and a \\write18 in one of
them must not run.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable

ENGINES = ("lualatex", "pdflatex", "xelatex")
DEFAULT_ENGINE = "lualatex"
TIMEOUT = 180.0
LOG_KEEP = 8000
SOURCE = "cv.tex"

# argv and working directory in; exit code and combined output out.
Runner = Callable[[list[str], pathlib.Path], tuple[int, str]]

_PAGES_IN_LOG = re.compile(r"Output written on .*?\((\d+) pages?", re.S)
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page[^s]")
_DOCUMENT = "\\begin{document}"


@dataclasses.dataclass(frozen=True)
class Build:
    ok: bool
    log: str
    pdf: bytes = b""
    pages: int | None = None


def build(tex: str, *, engine: str = DEFAULT_ENGINE, runner: Runner | None = None) -> Build:
    """Build in a scratch directory. Nothing outside it is read or written."""
    if engine not in ENGINES:
        return Build(ok=False, log=f"unknown LaTeX engine {engine!r}. use one of {', '.join(ENGINES)}.")
    run = runner or _run
    argv = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", SOURCE]
    with tempfile.TemporaryDirectory(prefix="jobhunt-cv-") as raw:
        folder = pathlib.Path(raw)
        (folder / SOURCE).write_text(tex, encoding="utf-8")
        # A CV usually needs a second pass for its own references to settle.
        code, log = run(argv, folder)
        if code == 0:
            code, log = run(argv, folder)
        pdf = folder / "cv.pdf"
        if code != 0 or not pdf.exists():
            return Build(ok=False, log=trim(log))
        data = pdf.read_bytes()
        return Build(ok=True, log=trim(log), pdf=data, pages=page_count(log, data))


def _run(argv: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    binary = shutil.which(argv[0])
    if binary is None:
        return 127, f"{argv[0]} was not found. install MacTeX or TeX Live."
    try:
        done = subprocess.run(
            [binary, *argv[1:]], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} ran past {int(TIMEOUT)} seconds and was stopped."
    return done.returncode, done.stdout + done.stderr


def page_count(log: str, pdf: bytes) -> int | None:
    found = _PAGES_IN_LOG.findall(log)
    if found:
        return int(found[-1])
    return len(_PAGE_OBJECT.findall(pdf)) or None


def trim(log: str, keep: int = LOG_KEEP) -> str:
    """LaTeX logs are long, and the part that says what broke is at the end."""
    return log if len(log) <= keep else "…\n" + log[-keep:]


def split_preamble(tex: str) -> tuple[str, str]:
    """Everything before `\\begin{document}`, and the rest.

    The same split `verify_cv.py` makes, so "the preamble is identical" means
    the same thing here as in the check that gates every tailored CV.
    """
    at = tex.find(_DOCUMENT)
    if at < 0:
        return tex, ""
    return tex[:at], tex[at:]
