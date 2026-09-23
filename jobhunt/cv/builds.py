"""Filling and building a template, with the trust it deserves.

Built-in templates ship with the package. Everything else is someone's file,
often from the internet. The difference is made here, once, so no caller can
forget it: an untrusted template is filled in a child process with a time limit
and built inside the sandbox, with the folders that hold the profile and the
database closed to it.
"""
from __future__ import annotations

import pathlib
import shutil
import threading

from jobhunt.config import Config
from jobhunt.cv import latex, render, sandbox
from jobhunt.cv import store as cvstore
from jobhunt.cv.model import Profile


def fill(source: str, profile: Profile, *, trusted: bool) -> str:
    if trusted:
        return render.render(profile, source)
    return render.render_isolated(profile, source)


def build(
    config: Config, tex: str, *, engine: str, trusted: bool, runner: latex.Runner | None = None
) -> latex.Build:
    if trusted:
        return latex.build(tex, engine=engine, runner=runner)
    return latex.build(
        tex,
        engine=engine,
        runner=runner,
        sandboxed=True,
        deny=private_dirs(config),
        cache=sandbox_cache(config),
    )


def private_dirs(config: Config) -> list[pathlib.Path]:
    """Where the profile, the master CV, the database and uploads live."""
    return [pathlib.Path(config.db_path).parent, cvstore.master_path(config).parent]


_SEEDING = threading.Lock()


def sandbox_cache(config: Config) -> pathlib.Path:
    """The font cache untrusted builds may write: a private copy of the shared
    one, seeded once so the first upload does not rebuild every font index."""
    folder = pathlib.Path(config.db_path).parent / "cv_sandbox" / "texmf-var"
    with _SEEDING:
        if not folder.exists():
            shared = sandbox.texmf_var()
            if shared is not None and shared.is_dir():
                shutil.copytree(shared, folder, symlinks=False)
            else:
                folder.mkdir(parents=True)
    return folder
