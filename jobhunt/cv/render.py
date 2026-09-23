"""Filling a CV template with a profile.

Templates are LaTeX with three Jinja delimiters nobody writes in LaTeX by hand:
\\BLOCK{…}, \\VAR{…} and \\#{…}. Jinja's own `{{ }}` and `{% %}` would collide
with the braces on every line of a resume, and line statements are off because
real preambles carry `%%` lines.

Two rules are enforced here rather than trusted to each template. Every value
is escaped unless it is already `Latex`, so a template that forgets a filter
prints "R\\&D" instead of breaking on "R&D". And a hidden item is a comment,
not an absence: the tailoring skill treats commented-out lines in the master as
deleted content and reads the notes above an entry. Both survive the move to
structured data only if the renderer writes them back as comments, which is
what `hidable` and `hidable_group` do.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable
from typing import Any

from jinja2 import StrictUndefined, TemplateError, TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from jobhunt.cv import markup
from jobhunt.cv.markup import Latex
from jobhunt.cv.model import ENTRY_SECTIONS, Basics, Profile


class RenderError(ValueError):
    """A template that cannot be filled, phrased for whoever picked it."""


@dataclasses.dataclass(frozen=True)
class Contact:
    label: str
    url: str


@dataclasses.dataclass(frozen=True)
class Section:
    key: str
    title: str
    kind: str  # summary | entries | skills | languages
    items: list[Any]


# Every line with anything on it but spaces and tabs. Not `\S`: a line that
# starts with some other whitespace is still a line TeX reads.
_LINE_START = re.compile(r"^([ \t]*)(?=[^ \t\n])", re.MULTILINE)
_INDENT = re.compile(r"[ \t]*")


def _comment_out(body: str) -> str:
    # After the indentation, so a commented block still lines up with the code
    # around it, the way the master was always edited by hand.
    return _LINE_START.sub(r"\1% ", body.replace("\r\n", "\n").replace("\r", "\n"))


def hidable(item: Any, caller: Callable[[], str]) -> Latex:
    """Wrap one entry or bullet: its notes above it, and comments if hidden."""
    body = caller()
    if getattr(item, "hidden", False):
        body = _comment_out(body)
    notes = (getattr(item, "notes", "") or "").strip()
    if notes:
        indent = _INDENT.match(body.lstrip("\n")).group()
        body = "".join(f"{indent}% {line}\n" for line in notes.splitlines()) + body
    return Latex(body)


def hidable_group(items: Iterable[Any], caller: Callable[[], str]) -> Latex:
    """Wrap a list's frame. When every item is hidden the frame goes too: an
    itemize with no visible \\item stops LaTeX with "missing \\item"."""
    body = caller()
    listed = list(items)
    if listed and all(getattr(item, "hidden", False) for item in listed):
        body = _comment_out(body)
    return Latex(body)


def _finalize(value: Any) -> Any:
    if isinstance(value, Latex):
        return value
    if value is None:
        return ""
    return markup.escape(str(value))


def environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        block_start_string="\\BLOCK{",
        block_end_string="}",
        variable_start_string="\\VAR{",
        variable_end_string="}",
        comment_start_string="\\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
        undefined=StrictUndefined,
        finalize=_finalize,
    )
    env.filters.update(
        rich=markup.rich, latex=markup.escape, url=markup.escape_url, comment=markup.comment
    )
    env.globals.update(hidable=hidable, hidable_group=hidable_group)
    return env


def contacts(basics: Basics) -> list[Contact]:
    """The header line, in the order the master has always printed it."""
    found = []
    if basics.phone:
        found.append(Contact(basics.phone, ""))
    if basics.email:
        found.append(Contact(basics.email, f"mailto:{basics.email}"))
    found += [Contact(link.label, link.url) for link in basics.links]
    if basics.location:
        found.append(Contact(basics.location, ""))
    return found


def sections(profile: Profile) -> list[Section]:
    """`layout` resolved into what each section prints, in print order. A
    section with nothing in it is left out rather than printed as a bare heading."""
    resolved = []
    for ref in profile.layout:
        kind, items = _contents(profile, ref.key)
        if items:
            resolved.append(Section(ref.key, ref.title, kind, items))
    return resolved


def _contents(profile: Profile, key: str) -> tuple[str, list[Any]]:
    if key == "summary":
        return "summary", [profile.summary] if profile.summary.text else []
    if key in ENTRY_SECTIONS:
        return "entries", list(getattr(profile, key))
    if key == "skills":
        return "skills", list(profile.skills)
    if key == "languages":
        return "languages", list(profile.languages)
    wanted = key.removeprefix("custom:")
    for custom in profile.custom_sections:
        if custom.id == wanted:
            return "entries", list(custom.entries)
    return "entries", []


def context(profile: Profile) -> dict[str, Any]:
    """Everything a template may use. Documented in the spec as the contract."""
    return {
        "profile": profile,
        "basics": profile.basics,
        "contacts": contacts(profile.basics),
        "extras": profile.extras,
        "summary": profile.summary,
        "experience": profile.experience,
        "education": profile.education,
        "projects": profile.projects,
        "skills": profile.skills,
        "languages": profile.languages,
        "custom_sections": profile.custom_sections,
        "sections": sections(profile),
    }


def render(profile: Profile, source: str) -> str:
    try:
        return environment().from_string(source).render(context(profile))
    except TemplateSyntaxError as exc:
        raise RenderError(f"the template does not parse, line {exc.lineno}: {exc.message}") from exc
    except UndefinedError as exc:
        raise RenderError(f"the template asks for something a profile does not have: {exc.message}") from exc
    except SecurityError as exc:
        raise RenderError(f"the template tried something templates may not do: {exc}") from exc
    except TemplateError as exc:
        raise RenderError(f"the template could not be filled: {exc}") from exc
