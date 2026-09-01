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
import typing
from typing import Any

import yaml

from jobhunt.config import Config
from jobhunt.pipeline import normalize as norm
from jobhunt.rank import regions
from jobhunt.rank.deterministic import FILTERS_FILENAME, PACKAGED_FILTERS, SENIORITY_ORDER

MANAGED_KEY = "_managed_by_jobhunt_config"

WORK_MODELS = ("remote", "hybrid", "onsite")
EMPLOYMENT_TYPES = norm.EMPLOYMENT_TYPES
DEFAULT_MARKETS = ("global_remote", "yc", "tr_local", "upwork")

# `ats` is a group, not an adapter: ticking twelve boxes is not the feature the
# user asked for. `upwork` is its own adapter with its own query settings below.
SOURCE_CHOICES: tuple[str, ...] = ("ats", "linkedin", "upwork")
LINKEDIN_SOURCE = "linkedin"
UPWORK_SOURCE = "upwork"
UPWORK_JOB_TYPES = ("hourly", "fixed")
UPWORK_EXPERIENCE = ("entry_level", "intermediate", "expert")
UPWORK_WORKLOAD = ("full_time", "part_time", "as_needed")


def _ats_sources() -> tuple[str, ...]:
    # Imported here, not at module scope: the linkedin adapter reaches back into
    # this module, and a top-level import would close that circle.
    from jobhunt import sources as source_registry

    # Upwork answers to its own explicit group, not the "everything else" one:
    # once it is in REGISTRY, ticking ATS in the browser must not silently start
    # an Upwork agent subprocess nobody asked for.
    excluded = {LINKEDIN_SOURCE, UPWORK_SOURCE}
    return tuple(sorted(name for name in source_registry.REGISTRY if name not in excluded))


def adapters_for(selection: list[str]) -> list[str]:
    """Group names to the adapter ids a run may actually call."""
    names: list[str] = []
    for group in selection:
        if group == "ats":
            names.extend(_ats_sources())
        elif group == LINKEDIN_SOURCE:
            names.append(LINKEDIN_SOURCE)
        elif group == UPWORK_SOURCE:
            names.append(UPWORK_SOURCE)
    return sorted(set(names))


class PreferenceError(ValueError):
    """A preference the user can fix, phrased for them rather than for a log."""


@dataclasses.dataclass
class UpworkPreferences:
    """What to ask Upwork for. Not an override of the salaried settings.

    Upwork is a different market with a different vocabulary: there is no
    location (every gig is remote), no seniority ladder, and a rate rather than
    a salary. Mirroring the shared fields here would give two places to answer
    one question.
    """

    queries: list[str] = dataclasses.field(default_factory=list)
    job_types: list[str] = dataclasses.field(default_factory=lambda: list(UPWORK_JOB_TYPES))
    min_hourly: float | None = None
    min_fixed: float | None = None
    experience_level: list[str] = dataclasses.field(default_factory=list)
    verified_payment_only: bool = True
    workload: list[str] = dataclasses.field(default_factory=list)
    proposals_max: int | None = None
    client_min_hires: int | None = None
    max_pages: int = 3


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
    # Which corpora a run may fetch from and shortlist out of. Both, deliberately:
    # a run that fetches only LinkedIn but shortlists everything cannot show what
    # LinkedIn alone is worth.
    sources: list[str] = dataclasses.field(default_factory=lambda: ["ats", "linkedin"])
    # Upwork's own query. Nested rather than flattened because none of these
    # keys mean anything to the other twelve sources.
    upwork: UpworkPreferences = dataclasses.field(default_factory=UpworkPreferences)

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


def _sources(values: list[str]) -> list[str]:
    """Validate a source selection, here and only here.

    A selection that expands to no adapter at all - nothing ticked - is rejected
    rather than stored: the run would fetch nothing while the shortlist
    restricted nothing, so the two would silently disagree about what the
    corpus is. The browser disables Save on an empty pick, but
    `jobhunt config set` and PUT /api/filters reach the same state and the
    backend has to answer for itself.
    """
    out = []
    for value in values:
        text = value.strip().lower()
        if text not in SOURCE_CHOICES:
            raise PreferenceError(
                f"unknown source {value!r}. use one of: {', '.join(SOURCE_CHOICES)}"
            )
        out.append(text)
    if not adapters_for(out):
        usable = [choice for choice in SOURCE_CHOICES if adapters_for([choice])]
        raise PreferenceError(
            f"that leaves no sources to search. use one or more of: {', '.join(usable)}"
        )
    return out


def _restricted(values: list[str], allowed: tuple[str, ...], label: str) -> list[str]:
    out = []
    for value in values:
        text = value.strip().lower()
        if text not in allowed:
            raise PreferenceError(f"unknown {label} {value!r}. use one of: {', '.join(allowed)}")
        out.append(text)
    return out


# Derived, not a second hand-written list: a field added to `UpworkPreferences`
# and forgotten here would otherwise round-trip silently instead of raising.
UPWORK_KEYS = {f.name for f in dataclasses.fields(UpworkPreferences)}


def _apply_upwork(target: UpworkPreferences, name: str, value: str) -> None:
    if name not in UPWORK_KEYS:
        raise PreferenceError(
            f"unknown setting upwork.{name!r}. known: "
            f"{', '.join(f'upwork.{k}' for k in sorted(UPWORK_KEYS))}"
        )
    blank = value in ("", "any", "none", "-")

    if name == "queries":
        target.queries = [] if blank else _split(value)
    elif name == "job_types":
        target.job_types = list(UPWORK_JOB_TYPES) if blank else _restricted(
            _split(value), UPWORK_JOB_TYPES, "upwork job type"
        )
    elif name == "min_hourly":
        target.min_hourly = None if blank else _number(value)
    elif name == "min_fixed":
        target.min_fixed = None if blank else _number(value)
    elif name == "experience_level":
        target.experience_level = [] if blank else _restricted(
            _split(value), UPWORK_EXPERIENCE, "upwork experience level"
        )
    elif name == "verified_payment_only":
        target.verified_payment_only = value.lower() not in ("false", "no", "0")
    elif name == "workload":
        target.workload = [] if blank else _restricted(_split(value), UPWORK_WORKLOAD, "upwork workload")
    elif name == "proposals_max":
        target.proposals_max = None if blank else int(_number(value))
    elif name == "client_min_hires":
        target.client_min_hires = None if blank else int(_number(value))
    elif name == "max_pages":
        target.max_pages = 3 if blank else int(_number(value))


def apply_updates(prefs: Preferences, updates: dict[str, str]) -> Preferences:
    """Apply `key=value` pairs from the CLI. Unknown keys are a loud error."""
    known = {
        "titles", "locations", "work_model", "job_types", "experience",
        "experience_max", "min_salary", "currency", "include_unstated_salary",
        "max_age_days", "top_n", "sources",
    }
    for key, raw in updates.items():
        if key.startswith("upwork."):
            _apply_upwork(prefs.upwork, key.removeprefix("upwork."), str(raw).strip())
            continue
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
        elif key == "sources":
            # No `blank` shortcut: "none" is a request for an empty corpus, which
            # `_sources` is the one place that refuses.
            prefs.sources = _sources([] if blank else _split(value))
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


# Levels whose postings name themselves differently from a permanent role:
# "Backend Intern" never contains "Backend Engineer", so the role titles alone
# never find it. Only the bottom two are listed. A senior or staff posting
# spells the role out in full, and a "senior" alternative would earn nothing
# while matching "Senior Manager, International Indirect Tax".
SENIORITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "intern": ("intern", "interns", "internship", "internships", "working student",
               "werkstudent", "trainee", "stajyer"),
    "junior": ("junior", "graduate", "grad", "new grad", "entry level", "entry-level"),
}

# Words that say nothing about the role. Left in, "full" would match "Full Time
# Intern" and "engineer" would match "Manufacturing Engineering Internship" -
# real internships, in fields nobody here asked for.
_GENERIC_TITLE_WORDS = frozenset({
    "engineer", "engineers", "engineering", "developer", "developers", "dev",
    "full", "senior", "junior", "intern", "internship", "staff", "lead", "principal",
    "the", "and", "of", "i", "ii", "iii",
})


def _role_tokens(titles: list[str]) -> list[str]:
    """The distinctive words of the titles asked for, in first-seen order.

    "Software Engineer" contributes "software": that is the half that survives
    in "Software Engineering Intern", where the role word has been declined into
    something the plain pattern would still catch but "Backend Intern" would not.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for title in titles:
        for word in re.split(r"[^\w+#]+", title.lower()):
            if len(word) < 2 or word in _GENERIC_TITLE_WORDS or word in seen:
                continue
            seen.add(word)
            tokens.append(word)
    return tokens


def _bounded(alternatives: tuple[str, ...] | list[str]) -> str:
    """An alternation matched as whole words, at both ends.

    The trailing boundary is the whole point here and is deliberately not what
    `title_patterns` does: "intern" without it matches "International", while
    "Software Engineer" *needs* the loose end to match "Software Engineering".
    """
    joined = "|".join(re.escape(str(value)) for value in alternatives)
    return rf"(?<!\w)(?:{joined})(?!\w)"


def title_patterns_for(prefs: Preferences) -> list[str]:
    """The title patterns for these preferences, seniority included.

    The keywords are generated here rather than written into `prefs.titles`, so
    what the user typed stays what the user typed and this rule can change
    later without a migration.
    """
    patterns = title_patterns(prefs.titles)
    if not patterns:
        return patterns

    tokens = _role_tokens(prefs.titles)
    if not tokens:
        return patterns

    floor = prefs.experience_min or SENIORITY_ORDER[0]
    ceiling = prefs.experience_max or SENIORITY_ORDER[-1]
    typed = " ".join(prefs.titles).lower()

    for level, keywords in SENIORITY_KEYWORDS.items():
        if not _level_in_range(level, floor, ceiling):
            continue
        # Asked for by hand already: the plain pattern covers it, and a second
        # alternative would only widen what that spelling was meant to pin down.
        if any(re.search(_bounded([keyword]), typed) for keyword in keywords):
            continue
        patterns.append(
            rf"(?i)^(?=.*{_bounded(keywords)})(?=.*{_bounded(tokens)}).*$"
        )
    return patterns


def _level_in_range(level: str, floor: str, ceiling: str) -> bool:
    if level not in SENIORITY_ORDER:
        return False
    at = SENIORITY_ORDER.index(level)
    low = SENIORITY_ORDER.index(floor) if floor in SENIORITY_ORDER else 0
    high = SENIORITY_ORDER.index(ceiling) if ceiling in SENIORITY_ORDER else len(SENIORITY_ORDER) - 1
    return low <= at <= high


def to_filters(prefs: Preferences, markets: tuple[str, ...] = DEFAULT_MARKETS) -> dict[str, Any]:
    """The managed block: exactly the keys this module owns, and nothing else."""
    codes, worldwide = regions.resolve(prefs.locations)

    profiles: dict[str, Any] = {}
    for market in markets:
        rules: dict[str, Any] = {}
        if market == UPWORK_SOURCE:
            # A gig has no country or employment type worth filtering on, and
            # Upwork's entry/intermediate/expert levels rate the contract's
            # difficulty, not a career stage: mapping them onto the salaried
            # seniority ladder would silently filter gigs. The rate floor is a
            # different quantity, handled where the adapter reads it.
            rules["require_titles_regex"] = title_patterns(prefs.upwork.queries) or [".*"]
            profiles[market] = rules
            continue
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

    selected = adapters_for(prefs.sources)
    everything = adapters_for(["ats", LINKEDIN_SOURCE])
    global_rules: dict[str, Any] = {
        "require_titles_regex": title_patterns_for(prefs),
        "max_age_days": prefs.max_age_days,
    }
    # Omitted when nothing is restricted, so an unchanged selection does not
    # churn the filter fingerprint every time an adapter is added.
    if selected != everything:
        global_rules["sources"] = selected

    return {
        "profiles": profiles,
        "global": global_rules,
        "digest": {"limit": prefs.top_n},
    }


def _coerce(declared: Any, value: Any) -> Any:
    """Rebuild a nested dataclass the YAML round trip flattened into a dict.

    `from_filters` reads a verbatim `dataclasses.asdict` mirror, so a nested
    field comes back as a plain dict. Left alone it would satisfy every type
    check and then fail on first attribute access, deep inside an adapter's
    `except Exception` - an empty corpus rather than a traceback.
    """
    origin = getattr(declared, "__origin__", None)
    if origin is dict and isinstance(value, dict):
        _, item_type = declared.__args__
        return {key: _coerce(item_type, item) for key, item in value.items()}
    if origin is list and isinstance(value, list):
        (item_type,) = declared.__args__
        return [_coerce(item_type, item) for item in value]
    if dataclasses.is_dataclass(declared) and isinstance(value, dict):
        fields = {f.name: f for f in dataclasses.fields(declared)}
        known = {k: _coerce(fields[k].type, v) for k, v in value.items() if k in fields}
        return declared(**known)
    return value


def from_filters(filters: dict[str, Any]) -> Preferences:
    """Read preferences back out, so the wizard can show current values.

    Only the managed block round-trips. A rule added by hand outside it is
    preserved on write but not shown here, because there is no honest way to
    render an arbitrary regex as a list of job titles.
    """
    managed = (filters or {}).get(MANAGED_KEY) or {}
    prefs = Preferences()
    # `Preferences` uses `from __future__ import annotations`, so a bare
    # `field.type` is a string. Resolve the hints once so `_coerce` can
    # dispatch on real types instead of silently doing nothing on a string.
    hints = typing.get_type_hints(Preferences)
    for field in dataclasses.fields(Preferences):
        if field.name in managed:
            setattr(prefs, field.name, _coerce(hints[field.name], managed[field.name]))
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
        owned = ["hard_requires", "seniority_min", "seniority_max", "salary", "allow_worldwide"]
        if market == UPWORK_SOURCE:
            # Upwork only ever owns its title pattern: a regenerated profile
            # must replace the old one rather than merge into it.
            owned = ["require_titles_regex"]
        for key in owned:
            profile.pop(key, None)
        profile.update(rules)
    for market in generated["profiles"]:
        document["profiles"].setdefault(market, {})

    # `update` can add a key but never remove one, and `to_filters` signals "no
    # source restriction" by omitting `sources` entirely. Without the pop, a
    # narrowed selection could never be widened again: the fetch path reads
    # preferences and resumes fetching ATS while the shortlist reads the stale
    # document and drops all of it as source_excluded.
    document.setdefault("global", {}).pop("sources", None)
    document["global"].update(generated["global"])
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

    patterns = [_re.compile(p) for p in title_patterns_for(prefs)]
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
