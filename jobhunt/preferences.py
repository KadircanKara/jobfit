"""Search preferences, and the one place they become filter rules.

The config wizard runs in Claude Code and is conversational, but a model writing
YAML by hand is fragile: one bad indent or one invented key and the filter
silently drops every job. So the wizard only collects answers and calls
`jobhunt config set`. This module validates them, translates them, and writes
the file.

The writer owns a delimited block inside filters.yaml. Everything outside it is
preserved byte for byte, so hand edits and the wizard can coexist.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Any

import yaml

from jobhunt.config import Config
from jobhunt.pipeline import normalize as norm
from jobhunt.rank import regions
from jobhunt.rank.deterministic import FILTERS_FILENAME, PACKAGED_FILTERS, SENIORITY_ORDER

MANAGED_KEY = "_managed_by_jobhunt_config"

WORK_MODELS = ("remote", "hybrid", "onsite")
EMPLOYMENT_TYPES = norm.EMPLOYMENT_TYPES
DEFAULT_MARKETS = ("global_remote", "yc", "tr_local")


class PreferenceError(ValueError):
    """A preference the user can fix, phrased for them rather than for a log."""


@dataclasses.dataclass
class Preferences:
    titles: list[str] = dataclasses.field(default_factory=list)
    locations: list[str] = dataclasses.field(default_factory=list)
    work_model: list[str] = dataclasses.field(default_factory=list)
    job_types: list[str] = dataclasses.field(default_factory=list)
    experience_min: str | None = None
    experience_max: str | None = None
    min_salary: float | None = None
    currency: str = "USD"
    include_unstated_salary: bool = True
    max_age_days: int = 30
    top_n: int = 15
    # Named selections of `titles`, so a set worth returning to can be picked
    # again after the field is cleared. Only the browser writes these; the
    # wizard neither shows nor asks about them.
    title_groups: dict[str, list[str]] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def display(self) -> list[tuple[str, str]]:
        """Rows for the wizard to show. Empty means "no restriction"."""
        def show(value: Any) -> str:
            if value is None or value == [] or value == "":
                return "any"
            if isinstance(value, list):
                return ", ".join(str(item) for item in value)
            return str(value)

        experience = "any"
        if self.experience_min and self.experience_max:
            experience = f"{self.experience_min} to {self.experience_max}"
        elif self.experience_min:
            experience = f"{self.experience_min}+"
        elif self.experience_max:
            experience = f"up to {self.experience_max}"

        salary = "any"
        if self.min_salary:
            unstated = "" if self.include_unstated_salary else ", stated only"
            salary = f"{self.min_salary:,.0f}+ {self.currency}{unstated}"

        return [
            ("titles", show(self.titles)),
            ("locations", show(self.locations)),
            ("work model", show(self.work_model)),
            ("job types", show(self.job_types)),
            ("experience", experience),
            ("min salary", salary),
            ("max age", f"{self.max_age_days} days"),
            ("show me", f"{self.top_n} per run"),
        ]


# --- parsing ------------------------------------------------------------------


def _split(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _seniority(value: str) -> str:
    """Accept how a person says it: "senior", "senior+", "Senior"."""
    text = str(value).strip().lower().rstrip("+")
    aliases = {"entry": "junior", "graduate": "junior", "middle": "mid", "medior": "mid"}
    text = aliases.get(text, text)
    if text not in SENIORITY_ORDER:
        raise PreferenceError(
            f"unknown experience level {value!r}. use one of: {', '.join(SENIORITY_ORDER)}"
        )
    return text


def _work_model(values: list[str]) -> list[str]:
    out = []
    for value in values:
        text = value.strip().lower().replace("-", "").replace(" ", "")
        text = {"onsite": "onsite", "office": "onsite", "inoffice": "onsite"}.get(text, text)
        if text not in WORK_MODELS:
            raise PreferenceError(
                f"unknown work model {value!r}. use one of: {', '.join(WORK_MODELS)}"
            )
        out.append(text)
    return out


def _job_types(values: list[str]) -> list[str]:
    out = []
    for value in values:
        text = norm.normalize_employment_type(value)
        if text is None:
            raise PreferenceError(
                f"unknown job type {value!r}. use one of: {', '.join(EMPLOYMENT_TYPES)}"
            )
        out.append(text)
    return out


def apply_updates(prefs: Preferences, updates: dict[str, str]) -> Preferences:
    """Apply `key=value` pairs from the CLI. Unknown keys are a loud error."""
    known = {
        "titles", "locations", "work_model", "job_types", "experience",
        "experience_max", "min_salary", "currency", "include_unstated_salary",
        "max_age_days", "top_n",
    }
    for key, raw in updates.items():
        if key not in known:
            raise PreferenceError(f"unknown setting {key!r}. known: {', '.join(sorted(known))}")
        value = str(raw).strip()
        blank = value in ("", "any", "none", "-")

        if key == "titles":
            prefs.titles = [] if blank else _split(value)
        elif key == "locations":
            prefs.locations = [] if blank else _split(value)
            missing = regions.unresolved(prefs.locations)
            if missing:
                raise PreferenceError(
                    f"could not place {', '.join(missing)}. use a country name, a two "
                    f"letter code, a region like Europe or Nordics, or 'worldwide'"
                )
        elif key == "work_model":
            prefs.work_model = [] if blank else _work_model(_split(value))
        elif key == "job_types":
            prefs.job_types = [] if blank else _job_types(_split(value))
        elif key == "experience":
            prefs.experience_min = None if blank else _seniority(value)
        elif key == "experience_max":
            prefs.experience_max = None if blank else _seniority(value)
        elif key == "min_salary":
            prefs.min_salary = None if blank else _number(value)
        elif key == "currency":
            prefs.currency = "USD" if blank else value.upper()[:4]
        elif key == "include_unstated_salary":
            prefs.include_unstated_salary = value.lower() not in ("false", "no", "0")
        elif key == "max_age_days":
            prefs.max_age_days = int(_number(value))
        elif key == "top_n":
            prefs.top_n = max(1, int(_number(value)))

    if prefs.experience_min and prefs.experience_max:
        if SENIORITY_ORDER.index(prefs.experience_min) > SENIORITY_ORDER.index(prefs.experience_max):
            raise PreferenceError(
                f"experience {prefs.experience_min} is above the ceiling {prefs.experience_max}"
            )
    return prefs


def _number(value: str) -> float:
    """Accept "80000", "80,000", "80k", "$80k"."""
    text = str(value).strip().lower().replace(",", "").replace("$", "").replace("€", "")
    multiplier = 1000.0 if text.endswith("k") else 1.0
    text = text.rstrip("k")
    try:
        return float(text) * multiplier
    except ValueError as exc:
        raise PreferenceError(f"{value!r} is not a number") from exc


# --- translation to filter rules ----------------------------------------------


def set_group(prefs: Preferences, name: str, titles: list[str]) -> Preferences:
    """Save `titles` under `name`, replacing a group of that name if there is one.

    Names collide case-insensitively: "test" and "Test" are one group, because
    two chips reading the same word with different capitals is a trap rather
    than a feature. The spelling last saved is the one kept.
    """
    label = name.strip()
    if not label:
        raise PreferenceError("A title group needs a name.")
    members = _dedupe_titles(titles)
    if not members:
        raise PreferenceError("A title group needs at least one title.")
    groups = {
        key: value for key, value in prefs.title_groups.items() if key.lower() != label.lower()
    }
    groups[label] = members
    prefs.title_groups = groups
    return prefs


def delete_group(prefs: Preferences, name: str) -> Preferences:
    """Forget one group. The titles currently in the field are untouched."""
    label = name.strip().lower()
    groups = {key: value for key, value in prefs.title_groups.items() if key.lower() != label}
    if len(groups) == len(prefs.title_groups):
        raise PreferenceError(f"There is no title group called {name!r}.")
    prefs.title_groups = groups
    return prefs


def _dedupe_titles(titles: list[str]) -> list[str]:
    """Keep the first spelling of each title, compared case-insensitively."""
    out: list[str] = []
    seen: set[str] = set()
    for title in titles:
        text = str(title).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def title_patterns(titles: list[str]) -> list[str]:
    """One case-insensitive, word-bounded alternation over the titles given.

    Escaped, because a user typing "C++" or "node.js" must not become a regex
    metacharacter that quietly matches everything or nothing.
    """
    if not titles:
        return []
    alternatives = "|".join(re.escape(title.strip()) for title in titles if title.strip())
    return [rf"(?i)(?<!\w)({alternatives})"]


def to_filters(prefs: Preferences, markets: tuple[str, ...] = DEFAULT_MARKETS) -> dict[str, Any]:
    """The managed block: exactly the keys this module owns, and nothing else."""
    codes, worldwide = regions.resolve(prefs.locations)

    profiles: dict[str, Any] = {}
    for market in markets:
        rules: dict[str, Any] = {}
        hard_requires: dict[str, Any] = {}
        if prefs.work_model:
            hard_requires["remote_type"] = list(prefs.work_model)
        if prefs.job_types:
            hard_requires["employment_type"] = list(prefs.job_types)
        # tr_local is Turkey by definition; overriding its country with a global
        # location preference would empty the market.
        if codes and market != "tr_local":
            hard_requires["country"] = codes
        if hard_requires:
            rules["hard_requires"] = hard_requires
        if prefs.experience_min:
            rules["seniority_min"] = prefs.experience_min
        if prefs.experience_max:
            rules["seniority_max"] = prefs.experience_max
        if prefs.min_salary:
            rules["salary"] = {
                "min_annual": prefs.min_salary,
                "currency": prefs.currency,
                "include_unstated": prefs.include_unstated_salary,
            }
        if worldwide:
            rules["allow_worldwide"] = True
        if rules:
            profiles[market] = rules

    return {
        "profiles": profiles,
        "global": {
            "require_titles_regex": title_patterns(prefs.titles),
            "max_age_days": prefs.max_age_days,
        },
        "digest": {"limit": prefs.top_n},
    }


def from_filters(filters: dict[str, Any]) -> Preferences:
    """Read preferences back out, so the wizard can show current values.

    Only the managed block round-trips. A rule added by hand outside it is
    preserved on write but not shown here, because there is no honest way to
    render an arbitrary regex as a list of job titles.
    """
    managed = (filters or {}).get(MANAGED_KEY) or {}
    prefs = Preferences()
    for field in dataclasses.fields(Preferences):
        if field.name in managed:
            setattr(prefs, field.name, managed[field.name])
    return prefs


# --- reading and writing filters.yaml -----------------------------------------


def filters_path(config: Config) -> pathlib.Path:
    return config.home / FILTERS_FILENAME


def load(config: Config) -> tuple[Preferences, dict[str, Any]]:
    """Returns (preferences, the whole filters document)."""
    path = filters_path(config)
    source = path if path.exists() else PACKAGED_FILTERS
    document = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return from_filters(document), document


def save(config: Config, prefs: Preferences) -> pathlib.Path:
    """Merge the managed block into filters.yaml, preserving everything else."""
    _, document = load(config)
    generated = to_filters(prefs)

    document.setdefault("profiles", {})
    for market, rules in generated["profiles"].items():
        profile = document["profiles"].setdefault(market, {})
        # Replace only the keys this module owns. A gate prompt path, a surface
        # threshold, or a hand-written hard_excludes list stays untouched.
        for key in ("hard_requires", "seniority_min", "seniority_max", "salary", "allow_worldwide"):
            profile.pop(key, None)
        profile.update(rules)
    for market in generated["profiles"]:
        document["profiles"].setdefault(market, {})

    document.setdefault("global", {}).update(generated["global"])
    document.setdefault("digest", {}).update(generated["digest"])
    document[MANAGED_KEY] = prefs.as_dict()

    path = filters_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# jobhunt filters. The block under _managed_by_jobhunt_config is written by\n"
        "# `jobhunt config set` and is overwritten on every run of it. Everything else\n"
        "# in this file is yours and is preserved.\n"
    )
    temp = path.with_suffix(".yaml.tmp")
    temp.write_text(
        header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    temp.replace(path)
    return path


def title_impact(config: Config, prefs: Preferences) -> tuple[int, int]:
    """(matching, total) active canonical jobs for the current title patterns.

    Setting titles replaces the shipped engineering pattern list, which can
    narrow the corpus much harder than intended: asking for "backend, AI" drops
    every posting called "Software Engineer". Printing the number turns an
    invisible narrowing into a visible one.
    """
    import re as _re

    from sqlalchemy import select

    from jobhunt.db.models import Job
    from jobhunt.db.session import session_scope

    patterns = [_re.compile(p) for p in title_patterns(prefs.titles)]
    with session_scope(config.db_path) as session:
        titles = [
            title
            for (title,) in session.execute(
                select(Job.title)
                .where(Job.is_active.is_(True))
                .where((Job.canonical_job_id == Job.id) | (Job.canonical_job_id.is_(None)))
            ).all()
        ]
    if not patterns:
        return len(titles), len(titles)
    matching = sum(1 for title in titles if any(p.search(title or "") for p in patterns))
    return matching, len(titles)
