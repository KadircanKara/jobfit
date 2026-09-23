"""The master CV: generated from the profile, never edited by hand.

Three states, and the rules between them are the point of this module:

- empty: nothing has been generated in the current template. That is the state
  before the first generate, and the state right after the template changes: a
  PDF in the old template is not the master any more, so it is not offered.
- ready: the files on disk were generated from the saved profile, in the
  current template.
- stale: same template, but the profile has been saved since. The files are
  still a faithful build of an older profile, so they stay downloadable, marked.

The files in CV_Source are only ever replaced by a generate that compiled. A
template change, a failed build or a broken template never touch them, because
tailoring and ranking read master.tex and must keep reading the last good one.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import threading
from typing import Any

from jobhunt import store as jobstore
from jobhunt.config import Config
from jobhunt.cv import builds, latex, render, templates
from jobhunt.cv import store as cvstore
from jobhunt.db.session import session_scope

RECORD_KEY = "cv.last_generated"

# One generate at a time, and no template change in the middle of one: two
# interleaved writes could leave one build's master.tex next to another's PDF
# under a record that calls the pair ready.
_LOCK = threading.Lock()


class MasterError(ValueError):
    """Something the person can fix, phrased for them."""


@dataclasses.dataclass(frozen=True)
class Status:
    state: str  # empty | ready | stale
    template_id: str
    template_name: str
    generated_at: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Outcome:
    ok: bool
    log: str
    pages: int | None
    status: Status
    # Characters the template's font could not print. The build succeeded, but
    # they are missing from the PDF.
    missing: tuple[str, ...] = ()


def status(config: Config) -> Status:
    template = templates.get(config, templates.default_id(config))
    record = _record(config)

    def empty(reason: str) -> Status:
        return Status("empty", template.id, template.name, None, reason)

    if record is None:
        return empty("Nothing has been generated yet.")
    if record.get("template_id") != template.id:
        return empty(f"The template changed to {template.name}. Generate to build your CV in it.")
    tex_path, pdf_path = cvstore.master_path(config), cvstore.master_pdf_path(config)
    if not (tex_path.exists() and pdf_path.exists()) or _sha(tex_path.read_bytes()) != record.get(
        "tex_sha256"
    ):
        return empty("master.tex was changed or removed outside the app. Generate to rebuild it.")
    saved = cvstore.read(config)
    if saved is None:
        return empty("There is no saved profile.")
    generated_at = record.get("generated_at")
    if saved.profile.fingerprint() != record.get("profile_hash"):
        return Status(
            "stale", template.id, template.name, generated_at,
            "Your details changed since this was generated.",
        )
    return Status("ready", template.id, template.name, generated_at, "")


def generate(config: Config, *, runner: latex.Runner | None = None) -> Outcome:
    """Render the saved profile in the default template, build it, and only if
    that worked, replace master.tex and Master_CV.pdf."""
    if not _LOCK.acquire(blocking=False):
        raise MasterError("the master CV is already being generated")
    try:
        return _generate(config, runner)
    finally:
        _LOCK.release()


def _generate(config: Config, runner: latex.Runner | None) -> Outcome:
    saved = cvstore.read(config)
    if saved is None:
        raise MasterError("save your details before generating the master CV")
    template = templates.get(config, templates.default_id(config))
    try:
        tex = builds.fill(template.text(), saved.profile, trusted=template.trusted)
    except render.RenderError as exc:
        return Outcome(ok=False, log=str(exc), pages=None, status=status(config))
    built = builds.build(config, tex, engine=template.engine, trusted=template.trusted, runner=runner)
    if not built.ok:
        return Outcome(ok=False, log=built.log, pages=None, status=status(config))

    source = tex.encode("utf-8")
    previous = _record(config) or {}
    changed = _replace(config, cvstore.master_path(config), source, "master")
    pdf_path = cvstore.master_pdf_path(config)
    # The PDF differs on every build (it carries its own timestamp), so an
    # identical rebuild is not written again - but only when the record proves
    # the PDF on disk came from this very source. A run that died between the
    # two writes leaves the new source next to the old PDF.
    proven = previous.get("tex_sha256") == _sha(source) and pdf_path.exists()
    if changed or not proven:
        _replace(config, pdf_path, built.pdf, "Master_CV")
    _write_record(
        config,
        {
            "template_id": template.id,
            "profile_hash": saved.profile.fingerprint(),
            "tex_sha256": _sha(source),
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        },
    )
    return Outcome(
        ok=True, log=built.log, pages=built.pages, status=status(config), missing=built.missing
    )


def use_template(config: Config, template_id: str) -> Status:
    """Make a template the default, and empty the master CV."""
    template = templates.get(config, template_id)
    with _LOCK, session_scope(config.db_path) as session:
        jobstore.meta_set(session, templates.DEFAULT_KEY, template.id)
        # Forgotten, not compared later: switching back to the previous template
        # is still a change, and the rule is that a change empties.
        jobstore.meta_set(session, RECORD_KEY, json.dumps({"template_id": None}))
    return status(config)


def download(config: Config, kind: str) -> pathlib.Path:
    current = status(config)
    if current.state == "empty":
        raise MasterError(current.reason)
    return cvstore.master_path(config) if kind == "tex" else cvstore.master_pdf_path(config)


def _replace(config: Config, path: pathlib.Path, data: bytes, prefix: str) -> bool:
    """Back up what is there, then write. False when it is already exactly this."""
    if path.exists() and path.read_bytes() == data:
        return False
    cvstore.take_backup(path, cvstore.backup_dir(config), prefix)
    cvstore.write_atomic(path, data)
    return True


def _record(config: Config) -> dict[str, Any] | None:
    with session_scope(config.db_path) as session:
        raw = jobstore.meta_get(session, RECORD_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_record(config: Config, record: dict[str, Any]) -> None:
    with session_scope(config.db_path) as session:
        jobstore.meta_set(session, RECORD_KEY, json.dumps(record))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
