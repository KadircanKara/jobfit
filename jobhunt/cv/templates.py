"""The CV templates: some ship with the package, the rest the user adds.

A template is a folder holding `template.tex.j2` and `meta.json`. Built-ins live
inside the package and are read-only. Uploads live next to the database, so
they move with it and never get written into the source tree. A user template
can never take a built-in's id, so "classic" always means the Classic that the
tailoring skill's rules were written against.

Which one is the default is a row in the `meta` table. A default that names a
template which has since gone falls back to Classic rather than failing every
generate.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import re
import threading
import unicodedata
import uuid

from jobhunt import store as jobstore
from jobhunt.config import Config
from jobhunt.cv import latex, model
from jobhunt.cv import store as cvstore
from jobhunt.db.session import session_scope

BUILTIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "cv_templates"
DEFAULT_ID = "classic"
DEFAULT_KEY = "cv.default_template"
SOURCE_NAME = "template.tex.j2"
CONTRACT_PATH = BUILTIN_DIR / "CONTRACT.md"


class TemplateError(ValueError):
    """A template that cannot be used, phrased for whoever picked it."""


class UnknownTemplate(TemplateError):
    """No template by that id."""


@dataclasses.dataclass(frozen=True)
class Template:
    id: str
    name: str
    source: str  # builtin | upload
    engine: str
    description: str
    folder: pathlib.Path

    @property
    def builtin(self) -> bool:
        return self.source == "builtin"

    @property
    def trusted(self) -> bool:
        """Only what ships with the package. Uploads fill in a child process and build in the sandbox."""
        return self.builtin

    def text(self) -> str:
        return (self.folder / SOURCE_NAME).read_text(encoding="utf-8")


def get_builtin(template_id: str) -> Template:
    template = _load(BUILTIN_DIR / template_id, "builtin")
    if template is None:
        raise UnknownTemplate(f"there is no built-in template called {template_id!r}")
    return template


def user_dir(config: Config) -> pathlib.Path:
    return pathlib.Path(config.db_path).parent / "cv_templates"


def all_templates(config: Config) -> list[Template]:
    found = _scan(BUILTIN_DIR, "builtin")
    taken = {template.id for template in found}
    found += [template for template in _scan(user_dir(config), "upload") if template.id not in taken]
    return found


def get(config: Config, template_id: str) -> Template:
    for template in all_templates(config):
        if template.id == template_id:
            return template
    raise UnknownTemplate(f"there is no template called {template_id!r}")


def default_id(config: Config) -> str:
    with session_scope(config.db_path) as session:
        chosen = jobstore.meta_get(session, DEFAULT_KEY)
    known = {template.id for template in all_templates(config)}
    return chosen if chosen in known else DEFAULT_ID


SAMPLE_NAME = "sample_profile.json"
NAME_LIMIT = 60
# Rename and remove touch the same folder: two tabs at once must not leave a
# removed template half-recreated by a rename that read it a moment earlier.
_CHANGING = threading.Lock()
TRASH = ".deleted"


def sample_profile() -> model.Profile:
    """A made-up person, so the gallery shows something before a profile exists."""
    return model.parse(json.loads((BUILTIN_DIR / SAMPLE_NAME).read_text(encoding="utf-8")))


def add(config: Config, name: str, source: str, *, engine: str, original: str | None = None) -> Template:
    name = _clean_name(name)
    folder = user_dir(config) / f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    cvstore.write_atomic(folder / SOURCE_NAME, source)
    if original is not None:
        # The file as it was uploaded, kept beside the template made from it.
        cvstore.write_atomic(folder / "original.tex", original)
    meta = {
        "name": name,
        "engine": latex.known_engine(engine),
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "description": "",
    }
    cvstore.write_atomic(folder / "meta.json", json.dumps(meta, indent=2) + "\n")
    return get(config, folder.name)


def rename(config: Config, template_id: str, name: str) -> Template:
    name = _clean_name(name)
    with _CHANGING:
        template = _upload(config, template_id)
        path = template.folder / "meta.json"
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise UnknownTemplate(f"there is no template called {template_id!r}") from exc
        meta["name"] = name
        cvstore.write_atomic(path, json.dumps(meta, indent=2) + "\n")
    return get(config, template_id)


def remove(config: Config, template_id: str) -> None:
    """Moved aside, never deleted: a template someone relied on can come back."""
    with _CHANGING:
        template = _upload(config, template_id)
        if default_id(config) == template.id:
            raise TemplateError("pick another default template before removing this one")
        trash = user_dir(config) / TRASH
        trash.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime(cvstore.BACKUP_STAMP)
        template.folder.rename(trash / f"{template.id}-{stamp}")


def _upload(config: Config, template_id: str) -> Template:
    template = get(config, template_id)
    if template.builtin:
        raise TemplateError(f"{template.name} ships with jobhunt and cannot be changed")
    return template


def _clean_name(name: str) -> str:
    name = " ".join(str(name).split())
    if not name:
        raise TemplateError("give the template a name")
    if len(name) > NAME_LIMIT:
        raise TemplateError(f"keep the name under {NAME_LIMIT} characters")
    return name


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:30] or "template"


def _scan(root: pathlib.Path, source: str) -> list[Template]:
    if not root.is_dir():
        return []
    found = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        template = _load(folder, source)
        if template is not None:
            found.append(template)
    return found


def _load(folder: pathlib.Path, source: str) -> Template | None:
    meta_path, body = folder / "meta.json", folder / SOURCE_NAME
    if not (meta_path.is_file() and body.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    engine = str(meta.get("engine") or latex.DEFAULT_ENGINE)
    return Template(
        id=folder.name,
        name=str(meta.get("name") or folder.name),
        source=source,
        engine=latex.known_engine(engine),
        description=str(meta.get("description") or ""),
        folder=folder,
    )
