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
import json
import pathlib

from jobhunt import store as jobstore
from jobhunt.config import Config
from jobhunt.cv import latex
from jobhunt.db.session import session_scope

BUILTIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "cv_templates"
DEFAULT_ID = "classic"
DEFAULT_KEY = "cv.default_template"
SOURCE_NAME = "template.tex.j2"


class TemplateError(ValueError):
    """A template that cannot be used, phrased for whoever picked it."""


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

    def text(self) -> str:
        return (self.folder / SOURCE_NAME).read_text(encoding="utf-8")


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
    raise TemplateError(f"there is no template called {template_id!r}")


def default_id(config: Config) -> str:
    with session_scope(config.db_path) as session:
        chosen = jobstore.meta_get(session, DEFAULT_KEY)
    known = {template.id for template in all_templates(config)}
    return chosen if chosen in known else DEFAULT_ID


def _scan(root: pathlib.Path, source: str) -> list[Template]:
    if not root.is_dir():
        return []
    found = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
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
        engine=engine if engine in latex.ENGINES else latex.DEFAULT_ENGINE,
        description=str(meta.get("description") or ""),
        folder=folder,
    )
