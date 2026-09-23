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
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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

_URL = re.compile(r"^(https?://|mailto:)[^\s{}\\]+$")


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
        raise ValueError("must start with http://, https:// or mailto: and contain no spaces or braces")
    return value


def _email(value: str) -> str:
    if value and "@" not in value:
        raise ValueError("does not look like an email address")
    return value


Url = Annotated[str, AfterValidator(_url)]
Id = Annotated[str, Field(min_length=1)]
Required = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Variant(_Model):
    id: Id
    label: str = ""
    text: Required


class Bullet(_Model):
    id: Id
    text: Required
    hidden: bool = False
    notes: str = ""


class Entry(_Model):
    id: Id
    title: str = ""
    subtitle: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    url: Url = ""
    gpa: str = ""
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
    label: Required
    url: Annotated[Url, Field(min_length=1)]


class Extra(_Model):
    id: Id
    label: Required
    value: Required


class SkillGroup(_Model):
    id: Id
    category: Required
    items: list[Required] = Field(default_factory=list)


class Language(_Model):
    id: Id
    name: Required
    level: str = ""
    detail: str = ""


class CustomSection(_Model):
    id: Id
    title: Required
    entries: list[Entry] = Field(default_factory=list)


class SectionRef(_Model):
    key: Required
    title: Required


class Basics(_Model):
    name: Required
    headline: str = ""
    headline_variants: list[Variant] = Field(default_factory=list)
    email: Annotated[str, AfterValidator(_email)] = ""
    phone: str = ""
    location: str = ""
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
