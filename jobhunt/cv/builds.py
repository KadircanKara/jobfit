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
from jobhunt.cv import latex, render, sandbox, templates
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
        seed=sandbox.texmf_var(),
    )


def make(
    config: Config,
    template: templates.Template,
    profile: Profile,
    *,
    runner: latex.Runner | None = None,
    source: str | None = None,
) -> tuple[str, latex.Build]:
    """The profile in this template, and its build, each with the trust and the
    engine the template carries. Raises RenderError when it cannot be filled.
    `source` is the template's text when the caller has already read it."""
    tex = fill(template.text() if source is None else source, profile, trusted=template.trusted)
    return tex, build(config, tex, engine=template.engine, trusted=template.trusted, runner=runner)


def private_dirs(config: Config) -> list[pathlib.Path]:
    """Where the profile, the master CV, the database and uploads live."""
    return [pathlib.Path(config.db_path).parent, cvstore.master_path(config).parent]
