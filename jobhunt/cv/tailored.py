"""The master a tailored CV is cut from, and how a tailored CV is built.

Each job in a tailoring batch can use its own template. So every application
folder gets its own copy of the master, rendered from the profile in the
template picked for that job: `master.tex`, with `master.json` saying which
template made it. The tailoring agent cuts from that copy, the verifier checks
the cut against it, and the reviewer and the revise studio read it as the
ground truth. A folder tailored before templates existed has no copy, and
falls back to the global master it was cut from.

A tailored CV is always built in the sandbox, whatever template it came from.
Its preamble is copied byte for byte from the master, so it may be an upload's,
and its body was written by an agent that read a posting from the internet.
Neither is ours to trust.

Run as a module, this is the compile command the tailoring agent is given in
place of calling lualatex itself:

    python -m jobhunt.cv.tailored [--config PATH] cv.tex
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

from jobhunt import config as config_module
from jobhunt.config import Config
from jobhunt.cv import builds, latex, render, templates
from jobhunt.cv import store as cvstore

SNAPSHOT_NAME = "master.tex"
RECORD_NAME = "master.json"

# How much of a failed build's log a refusal quotes.
LOG_TAIL = 800

# The command line builds for real. Tests swap in a fake runner here.
_RUNNER: latex.Runner | None = None


class TailoredError(ValueError):
    """A master that cannot be made for a job, phrased for whoever picked it."""


@dataclasses.dataclass(frozen=True)
class Snapshot:
    template: templates.Template
    tex: str


def snapshot(config: Config, template_id: str, *, runner: latex.Runner | None = None) -> Snapshot:
    """The saved profile in this template, built once to prove it compiles.

    A template that cannot build is refused here, before a folder exists, so a
    bad pick costs a message rather than an agent run cut from a broken master.
    """
    try:
        saved = cvstore.read(config)
    except cvstore.StoreError as exc:
        raise TailoredError(str(exc)) from exc
    if saved is None:
        raise TailoredError("there is no saved profile to build a master from. Save one on the Profile page.")
    try:
        template = templates.get(config, template_id)
    except templates.TemplateError as exc:
        raise TailoredError(str(exc)) from exc
    try:
        tex = builds.fill(template.text(), saved.profile, trusted=template.trusted)
    except render.RenderError as exc:
        raise TailoredError(f"{template.name} could not be filled in: {exc}") from exc
    built = builds.build(config, tex, engine=template.engine, trusted=template.trusted, runner=runner)
    if not built.ok:
        raise TailoredError(f"{template.name} does not build with your profile:\n{built.log[-LOG_TAIL:]}")
    return Snapshot(template=template, tex=tex)


def write(folder: pathlib.Path | str, snap: Snapshot) -> pathlib.Path:
    """Put the snapshot in an application folder, with a record of its template."""
    folder = pathlib.Path(folder)
    template = snap.template
    record = {"template_id": template.id, "template_name": template.name, "engine": template.engine}
    cvstore.write_atomic(folder / SNAPSHOT_NAME, snap.tex)
    cvstore.write_atomic(folder / RECORD_NAME, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return folder / SNAPSHOT_NAME


def master_for(config: Config, folder: pathlib.Path | str) -> pathlib.Path:
    """The master this folder's CV is cut from: its own, else the global one."""
    own = pathlib.Path(folder) / SNAPSHOT_NAME
    return own if own.is_file() else cvstore.master_path(config)


def engine_for(tex_path: pathlib.Path) -> str:
    """The engine of the template the folder's master came from. A folder with
    no record was cut from the global master, which is built with lualatex."""
    try:
        record = json.loads((tex_path.parent / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return latex.DEFAULT_ENGINE
    engine = record.get("engine") if isinstance(record, dict) else None
    return latex.known_engine(str(engine or ""))


def build(config: Config, tex_path: pathlib.Path, *, runner: latex.Runner | None = None) -> latex.Build:
    """Build a tailored CV: in the sandbox, with its template's engine. Nothing is written."""
    tex = tex_path.read_text(encoding="utf-8")
    return builds.build(config, tex, engine=engine_for(tex_path), trusted=False, runner=runner)


def compile_here(
    config: Config, tex_path: pathlib.Path, *, runner: latex.Runner | None = None
) -> latex.Build:
    """Build, then leave the PDF and the log next to the source, where the
    tailoring skill's verifiers look for them. A failed build removes the last
    round's PDF, so nothing checks a CV that no longer matches its source."""
    built = build(config, tex_path, runner=runner)
    tex_path.with_suffix(".log").write_text(built.transcript or built.log, encoding="utf-8")
    pdf = tex_path.with_suffix(".pdf")
    if built.ok:
        pdf.write_bytes(built.pdf)
    else:
        pdf.unlink(missing_ok=True)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jobhunt.cv.tailored",
        description="Build a tailored CV the way the app does: in the sandbox, with its template's engine.",
    )
    parser.add_argument("tex", type=pathlib.Path, help="the tailored source, usually cv.tex")
    parser.add_argument("--config", type=pathlib.Path, default=None, help="the app's config.yaml")
    args = parser.parse_args(argv)

    tex = args.tex.expanduser().resolve()
    if not tex.is_file():
        print(f"no such file: {tex}", file=sys.stderr)
        return 2
    config = config_module.load(args.config)
    built = compile_here(config, tex, runner=_RUNNER)
    if not built.ok:
        print(built.log, file=sys.stderr)
        return 1
    pages = f"{built.pages} page{'' if built.pages == 1 else 's'}" if built.pages else "unknown length"
    print(f"built {tex.with_suffix('.pdf')} · {pages} · {tex.with_suffix('.log').name} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
