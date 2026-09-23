"""Filling and building a template, with the trust it deserves.

Built-in templates ship with the package. Everything else is someone's file,
often from the internet. The difference is made here, once, so no caller can
forget it: an untrusted template is filled in a child process with a time limit
and built inside the sandbox, with the folders that hold the profile and the
database closed to it.
"""
from __future__ import annotations

import pathlib

from jobhunt.config import Config
from jobhunt.cv import latex, render
from jobhunt.cv import store as cvstore
from jobhunt.cv.model import Profile


def fill(source: str, profile: Profile, *, trusted: bool) -> str:
    if trusted:
        return render.render(profile, source)
    return render.render_isolated(profile, source)


def build(
    config: Config, tex: str, *, engine: str, trusted: bool, runner: latex.Runner | None = None
) -> latex.Build:
    return latex.build(tex, engine=engine, runner=runner, sandboxed=not trusted, deny=private_dirs(config))


def private_dirs(config: Config) -> list[pathlib.Path]:
    """Where the profile, the master CV, the database and uploads live."""
    return [pathlib.Path(config.db_path).parent, cvstore.master_path(config).parent]
