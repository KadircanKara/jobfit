"""master.tex: read, edit, back up, compile.

The master is the single source every tailored CV is cut from, and the
anti-fabrication rule the reviewer enforces is only as good as this file. So
every write takes a timestamped copy first, backups are never rewritten or
deleted here, and a restore is itself a write — which means it takes a backup
too, and undoing a restore stays possible.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable

from jobhunt.config import Config

BACKUP_STAMP = "%Y%m%d-%H%M%S"
BACKUP_NAME = re.compile(r"^master-\d{8}-\d{6}(-\d+)?\.tex$")
LATEX_TIMEOUT = 120.0

Runner = Callable[[pathlib.Path, pathlib.Path], tuple[int, str, pathlib.Path | None]]


class ProfileError(ValueError):
    """Something the user can fix, phrased for them."""


@dataclasses.dataclass(frozen=True)
class Master:
    path: pathlib.Path
    text: str
    modified_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class Backup:
    name: str
    path: pathlib.Path
    taken_at: dt.datetime
    size: int


@dataclasses.dataclass(frozen=True)
class CompileResult:
    ok: bool
    log: str
    pdf_bytes: bytes = b""


def master_path(config: Config) -> pathlib.Path:
    return pathlib.Path(str(config.get("tailoring", "master_tex"))).expanduser()


def backup_dir(config: Config) -> pathlib.Path:
    return master_path(config).parent / "backups"


def read(config: Config) -> Master:
    path = master_path(config)
    if not path.exists():
        raise ProfileError(f"no master CV at {path}")
    stat = path.stat()
    return Master(
        path=path,
        text=path.read_text(encoding="utf-8"),
        modified_at=dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC),
    )


def backups(config: Config) -> list[Backup]:
    """Newest first. Only files this module wrote."""
    folder = backup_dir(config)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        if not BACKUP_NAME.match(path.name):
            continue
        stat = path.stat()
        found.append(
            Backup(
                name=path.name,
                path=path,
                taken_at=dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC),
                size=stat.st_size,
            )
        )
    # By time, not by name: two saves inside the same second differ only by a
    # "-1" suffix, and "-" sorts before "." so the names would come back in the
    # wrong order exactly when the two are hardest to tell apart.
    return sorted(found, key=lambda backup: (backup.taken_at, backup.name), reverse=True)


def take_backup(config: Config) -> Backup | None:
    """Copy the current master aside. Returns None when there is nothing yet."""
    source = master_path(config)
    if not source.exists():
        return None
    folder = backup_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime(BACKUP_STAMP)
    target = folder / f"master-{stamp}.tex"
    # Two saves inside the same second must not collide, or one of them is lost.
    suffix = 1
    while target.exists():
        target = folder / f"master-{stamp}-{suffix}.tex"
        suffix += 1
    shutil.copy2(source, target)
    stat = target.stat()
    return Backup(
        name=target.name,
        path=target,
        taken_at=dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC),
        size=stat.st_size,
    )


def write(config: Config, text: str) -> Backup | None:
    """Back up, then replace the master."""
    if not text or not text.strip():
        raise ProfileError("an empty master would break every future tailoring run")
    backup = take_backup(config)
    path = master_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return backup


def restore(config: Config, name: str) -> Backup | None:
    """Put a backup back, taking a backup of what is there now first."""
    if not BACKUP_NAME.match(name):
        raise ProfileError(f"{name!r} is not one of the backups")
    source = backup_dir(config) / name
    if not source.is_file():
        raise ProfileError(f"no backup named {name}")
    return write(config, source.read_text(encoding="utf-8"))


def _latex_runner(tex_path: pathlib.Path, out_dir: pathlib.Path):
    binary = shutil.which("pdflatex") or shutil.which("xelatex")
    if binary is None:
        return 127, "no LaTeX toolchain found. install MacTeX or TeX Live.", None
    done = subprocess.run(
        [binary, "-interaction=nonstopmode", "-halt-on-error",
         f"-output-directory={out_dir}", str(tex_path)],
        capture_output=True, text=True, timeout=LATEX_TIMEOUT, check=False,
    )
    pdf = out_dir / (tex_path.stem + ".pdf")
    return done.returncode, done.stdout + done.stderr, pdf if pdf.exists() else None


def compile_tex(config: Config, *, text: str | None = None, runner: Runner | None = None) -> CompileResult:
    """Build the master in a temp directory.

    Compiling never touches the saved file: a draft that does not build should
    cost you the preview, not the CV you already had.
    """
    source = text if text is not None else read(config).text
    run = runner or _latex_runner

    with tempfile.TemporaryDirectory() as raw:
        out_dir = pathlib.Path(raw)
        tex_path = out_dir / "master.tex"
        tex_path.write_text(source, encoding="utf-8")

        # A CV usually needs two passes for its own references to settle.
        code, log, pdf = run(tex_path, out_dir)
        if code == 0:
            code, log, pdf = run(tex_path, out_dir)

        if code != 0 or pdf is None or not pdf.exists():
            return CompileResult(ok=False, log=_trim(log))
        return CompileResult(ok=True, log=_trim(log), pdf_bytes=pdf.read_bytes())


def _trim(log: str, keep: int = 8000) -> str:
    """LaTeX logs are long and the useful part is at the end."""
    return log if len(log) <= keep else "…\n" + log[-keep:]
