"""Compiling a rendered CV.

One place builds LaTeX for the CV builder, so every PDF comes out of the same
engine with the same flags. lualatex is the default because it is the engine
the tailoring skill compiles with: a master built any other way would not be
the document the tailored CVs are cut from and compared against.

`-no-shell-escape` is passed every time rather than left to the TeX
installation's default. Templates can be uploaded, and a \\write18 in one of
them must not run. lualatex also gets `--nosocket` and an environment with
nothing in it but what TeX needs: the server's own environment carries API keys
from .env, and Lua inside a template could otherwise print them into the PDF.

What this does not do is confine reads. `--safer` breaks luaotfload (Classic's
FontAwesome icons stop loading) and `openin_any=p` stops lualatex from starting
at all, so a template can still read files the user can read. Built-in
templates are trusted; uploaded ones need an OS-level sandbox before they run.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable

from jobhunt.cv import sandbox

ENGINES = ("lualatex", "pdflatex", "xelatex")
DEFAULT_ENGINE = "lualatex"
TIMEOUT = 180.0
LOG_KEEP = 8000
SOURCE = "cv.tex"

# argv and working directory in; exit code and combined output out.
Runner = Callable[[list[str], pathlib.Path], tuple[int, str]]

_PAGES_IN_LOG = re.compile(r"Output written on .*?\((\d+) pages?", re.S)
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page[^s]")
# What TeX logs, rather than fails on, when a font has no glyph for a character.
_MISSING = re.compile(r"Missing character: There is no (.+?) \(U\+[0-9A-F]+\)")
_KEEP_ENV = ("HOME", "LANG", "LC_ALL", "TMPDIR")
_TEX_ENV = ("TEXMF", "TEXINPUTS", "OSFONTDIR")
_DOCUMENT = "\\begin{document}"


@dataclasses.dataclass(frozen=True)
class Build:
    ok: bool
    log: str
    pdf: bytes = b""
    pages: int | None = None
    # Characters the build dropped because the template's font cannot print
    # them. The build still succeeds, so these are said out loud instead.
    missing: tuple[str, ...] = ()


def build(
    tex: str,
    *,
    engine: str = DEFAULT_ENGINE,
    runner: Runner | None = None,
    sandboxed: bool = False,
    deny: Iterable[pathlib.Path] = (),
    cache: pathlib.Path | None = None,
) -> Build:
    """Build in a scratch directory. Nothing outside it is read or written."""
    if engine not in ENGINES:
        return Build(ok=False, log=f"unknown LaTeX engine {engine!r}. use one of {', '.join(ENGINES)}.")
    run = runner or _run
    argv = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape"]
    if engine == "lualatex":
        argv.append("--nosocket")
    argv.append(SOURCE)
    with tempfile.TemporaryDirectory(prefix="jobhunt-cv-") as raw:
        folder = pathlib.Path(raw)
        (folder / SOURCE).write_text(tex, encoding="utf-8")
        if sandboxed:
            if runner is None and not sandbox.available():
                return Build(
                    ok=False,
                    log="uploaded templates only build inside the macOS sandbox (sandbox-exec), "
                    "which is not available here, so this one was not run.",
                )
            # TeX must be able to write a font cache, and the shared one is
            # read-only in the sandbox. Without a private cache to reuse, this
            # build gets a throwaway one: correct, only slower.
            argv = sandbox.wrap(argv, folder, deny, cache or folder / "texmf-var")
        # A CV usually needs a second pass for its own references to settle.
        code, log = run(argv, folder)
        if code == 0:
            code, log = run(argv, folder)
        pdf = folder / "cv.pdf"
        if code != 0 or not pdf.exists():
            return Build(ok=False, log=trim(log))
        data = pdf.read_bytes()
        return Build(
            ok=True, log=trim(log), pdf=data, pages=page_count(log, data), missing=missing(folder / "cv.log")
        )


def _run(argv: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    binary = shutil.which(argv[0])
    if binary is None:
        return 127, f"{argv[0]} was not found. install MacTeX or TeX Live."
    try:
        done = subprocess.run(
            [binary, *argv[1:]], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT, check=False,
            env=environment(binary, *(arg for arg in argv[1:] if os.path.isabs(arg))),
        )
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} ran past {int(TIMEOUT)} seconds and was stopped."
    return done.returncode, done.stdout + done.stderr


def environment(*binaries: str) -> dict[str, str]:
    """Only what TeX needs to find itself and its font cache."""
    kept = {key: value for key, value in os.environ.items() if key in _KEEP_ENV or key.startswith(_TEX_ENV)}
    folders = [str(pathlib.Path(binary).parent) for binary in binaries]
    kept["PATH"] = os.pathsep.join([*dict.fromkeys(folders), "/usr/bin", "/bin"])
    return kept


def missing(log_file: pathlib.Path) -> tuple[str, ...]:
    """The characters the log says had no glyph, each once, in order."""
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(dict.fromkeys(_MISSING.findall(text)))


def known_engine(name: str) -> str:
    """`name` if it is an engine this app runs, otherwise the default."""
    return name if name in ENGINES else DEFAULT_ENGINE


def page_count(log: str, pdf: bytes) -> int | None:
    # The last line, because a two-pass build logs one per pass.
    found = _PAGES_IN_LOG.findall(log or "")
    if found:
        return int(found[-1])
    return len(_PAGE_OBJECT.findall(pdf or b"")) or None


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
