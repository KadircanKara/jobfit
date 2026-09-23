"""The candidate's CV as data.

Until now the master CV was a LaTeX file edited by hand, so the only way to
change a date was to find it between two macros. This is the same content as
fields. `master.tex` is now something the app writes from it.

Two properties matter beyond the obvious ones. Every entry and bullet has a
stable id that survives reordering, so a later tailoring step can say which
line of the master a line of a CV came from. And `hidden` is not deletion:
hidden items stay in the document and are written into master.tex as comments,
because the tailoring skill treats commented-out content as removed-on-purpose
and reads the notes above an entry.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from jobhunt.cv import markup

SCHEMA_VERSION = 1

# The sections every profile has, in the order the master has always printed
# them. Custom sections join the layout as "custom:<id>".
FIXED_SECTIONS = ("summary", "experience", "education", "skills", "languages", "projects")
ENTRY_SECTIONS = ("experience", "education", "projects")
DEFAULT_TITLES = {
    "summary": "PROFESSIONAL SUMMARY",
    "experience": "EXPERIENCE",
    "education": "EDUCATION",
    "skills": "SKILLS",
    "languages": "LANGUAGES",
    "projects": "PROJECTS",
}

_URL = re.compile(rf"^(https?://|mailto:)[{markup.URL_CHARS}]+$")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_BREAK = re.compile(r"[\r\n\u2028\u2029\x85]+")


class ProfileInvalid(ValueError):
    """Every problem with a profile, each with the dotted path of its field."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__(problems[0][1])
        self.problems = problems

    @property
    def field(self) -> str:
        return self.problems[0][0]

    @property
    def message(self) -> str:
        return self.problems[0][1]


def _url(value: str) -> str:
    if value and not _URL.match(value):
        raise ValueError(
            "must start with http://, https:// or mailto: and use only URL characters "
            "(no spaces, carets or braces)"
        )
    return value


def _email(value: str) -> str:
    if value and not _EMAIL.match(value):
        raise ValueError("does not look like an email address")
    return value


def _one_line(value: Any) -> Any:
    # Most fields print inside one macro argument or one comment line, where a
    # line break either ends the paragraph mid-argument (\resumeItem is not a
    # long macro, so the build stops) or lets a hidden line out of its comment.
    # An accidental Enter in the form is joined back up rather than refused.
    if isinstance(value, str):
        return " ".join(part.strip() for part in _BREAK.split(value) if part.strip())
    return value


Url = Annotated[str, AfterValidator(_url)]
Id = Annotated[str, Field(min_length=1)]
Required = Annotated[str, Field(min_length=1)]
Line = Annotated[str, BeforeValidator(_one_line)]
RequiredLine = Annotated[str, BeforeValidator(_one_line), Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Variant(_Model):
    id: Id
    label: Line = ""
    text: Required


class LineVariant(Variant):
    """A variant printed on one line, like a headline."""

    text: RequiredLine


class Bullet(_Model):
    id: Id
    text: RequiredLine
    hidden: bool = False
    notes: str = ""


class Entry(_Model):
    id: Id
    title: Line = ""
    subtitle: Line = ""
    location: Line = ""
    start: Line = ""
    end: Line = ""
    url: Url = ""
    gpa: Line = ""
    hidden: bool = False
    notes: str = ""
    bullets: list[Bullet] = Field(default_factory=list)

    @property
    def dates(self) -> str:
        return " - ".join(part for part in (self.start, self.end) if part)

    @model_validator(mode="after")
    def _says_something(self) -> Entry:
        if not self.title and not self.bullets:
            raise ValueError("needs a title or at least one bullet")
        return self


class Link(_Model):
    label: RequiredLine
    url: Annotated[Url, Field(min_length=1)]


class Extra(_Model):
    id: Id
    label: RequiredLine
    value: RequiredLine


class SkillGroup(_Model):
    id: Id
    category: RequiredLine
    items: list[RequiredLine] = Field(default_factory=list)
    # A skills line carries notes and can be hidden like any bullet: the master
    # keeps guidance above its skills lines too, and the import must not drop it.
    hidden: bool = False
    notes: str = ""


class Language(_Model):
    id: Id
    name: RequiredLine
    level: Line = ""
    detail: Line = ""


class CustomSection(_Model):
    id: Id
    title: RequiredLine
    entries: list[Entry] = Field(default_factory=list)


class SectionRef(_Model):
    key: Required
    title: RequiredLine


class Basics(_Model):
    name: RequiredLine
    headline: Line = ""
    headline_variants: list[LineVariant] = Field(default_factory=list)
    email: Annotated[str, AfterValidator(_email)] = ""
    phone: Line = ""
    location: Line = ""
    links: list[Link] = Field(default_factory=list)


class Summary(_Model):
    text: str = ""
    variants: list[Variant] = Field(default_factory=list)


def default_layout() -> list[SectionRef]:
    return [SectionRef(key=key, title=DEFAULT_TITLES[key]) for key in FIXED_SECTIONS]


class Profile(_Model):
    schema_version: int = SCHEMA_VERSION
    basics: Basics
    extras: list[Extra] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    experience: list[Entry] = Field(default_factory=list)
    education: list[Entry] = Field(default_factory=list)
    projects: list[Entry] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    custom_sections: list[CustomSection] = Field(default_factory=list)
    layout: list[SectionRef] = Field(default_factory=default_layout)

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value > SCHEMA_VERSION:
            raise ValueError(f"schema_version {value} was written by a newer version of jobhunt")
        return value

    def fingerprint(self) -> str:
        """Which version of the profile this is, independent of key order."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse(data: Any) -> Profile:
    """A profile, or every reason it is not one."""
    try:
        profile = Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileInvalid([(_path(error["loc"]), _say(error)) for error in exc.errors()]) from exc
    problems = _duplicate_ids(profile) + _layout_problems(profile)
    if problems:
        raise ProfileInvalid(problems)
    return profile


_MESSAGES = {
    "missing": "is required",
    "string_too_short": "cannot be empty",
    "too_short": "cannot be empty",
    "extra_forbidden": "is not a field a profile has",
}


def _path(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) or "profile"


def _say(error: dict[str, Any]) -> str:
    if error["type"] in _MESSAGES:
        return _MESSAGES[error["type"]]
    return str(error["msg"]).removeprefix("Value error, ")


def _ids(profile: Profile):
    for index, variant in enumerate(profile.basics.headline_variants):
        yield f"basics.headline_variants.{index}.id", variant.id
    for index, variant in enumerate(profile.summary.variants):
        yield f"summary.variants.{index}.id", variant.id
    for index, extra in enumerate(profile.extras):
        yield f"extras.{index}.id", extra.id
    for key in ENTRY_SECTIONS:
        for index, entry in enumerate(getattr(profile, key)):
            yield from _entry_ids(f"{key}.{index}", entry)
    for index, group in enumerate(profile.skills):
        yield f"skills.{index}.id", group.id
    for index, language in enumerate(profile.languages):
        yield f"languages.{index}.id", language.id
    for index, custom in enumerate(profile.custom_sections):
        yield f"custom_sections.{index}.id", custom.id
        for inner, entry in enumerate(custom.entries):
            yield from _entry_ids(f"custom_sections.{index}.entries.{inner}", entry)


def _entry_ids(path: str, entry: Entry):
    yield f"{path}.id", entry.id
    for index, bullet in enumerate(entry.bullets):
        yield f"{path}.bullets.{index}.id", bullet.id


def _duplicate_ids(profile: Profile) -> list[tuple[str, str]]:
    seen: set[str] = set()
    problems = []
    for path, value in _ids(profile):
        if value in seen:
            problems.append((path, f"id {value!r} is used twice"))
        seen.add(value)
    return problems


def _layout_problems(profile: Profile) -> list[tuple[str, str]]:
    allowed = set(FIXED_SECTIONS) | {f"custom:{custom.id}" for custom in profile.custom_sections}
    seen: set[str] = set()
    problems = []
    for index, ref in enumerate(profile.layout):
        if ref.key not in allowed:
            problems.append((f"layout.{index}.key", f"{ref.key!r} is not a section this profile has"))
        elif ref.key in seen:
            problems.append((f"layout.{index}.key", f"{ref.key!r} appears twice"))
        seen.add(ref.key)
    return problems
