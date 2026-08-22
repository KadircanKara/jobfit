"""Suggestions for the two tag fields, counted against the live corpus.

The counts are the point. Dropping a title silently deletes every job it would
have matched, and the CLI only reports that after the fact. Here the number
sits next to the suggestion before it is chosen.
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from sqlalchemy import case, func, select

from jobhunt.config import Config
from jobhunt.db import models
from jobhunt.db.session import session_scope
from jobhunt.rank import regions

# Titles worth offering an engineer. Counted live, so a title nobody is hiring
# for shows a zero rather than looking like a safe choice.
TITLE_SUGGESTIONS = (
    "AI Engineer", "Applied AI Engineer", "Machine Learning Engineer", "LLM Engineer",
    "GenAI Engineer", "Generative AI Engineer", "Forward Deployed Engineer",
    "Prompt Engineer", "RAG Engineer", "Agent Engineer", "AI Solutions Architect",
    "MLOps Engineer", "Software Engineer", "Backend Engineer", "Full-Stack Engineer",
    "Fullstack Engineer", "Frontend Engineer", "Platform Engineer",
    "Infrastructure Engineer", "Python Engineer", "Data Engineer", "Data Scientist",
    "Site Reliability Engineer", "DevOps Engineer", "Cloud Engineer",
    "Research Engineer", "Solutions Engineer", "Integration Engineer",
    "Product Engineer", "Systems Engineer",
)

REGION_LABELS = {
    "europe": "region", "eu": "region", "emea": "region", "nordics": "region",
    "dach": "region", "benelux": "region", "apac": "region", "asia": "region",
    "africa": "region", "north america": "region", "latin america": "region",
    "middle east": "region",
}


def _country_counts(config: Config) -> dict[str, int]:
    with session_scope(config.db_path) as session:
        rows = session.execute(
            select(models.Job.country, func.count(models.Job.id))
            .where(models.Job.is_active.is_(True))
            .group_by(models.Job.country)
        ).all()
    return {code: count for code, count in rows if code}


def _title_counts(config: Config) -> dict[str, int]:
    """Every suggestion counted in one pass over the corpus.

    A count per suggestion meant a scan per suggestion: `title LIKE '%x%'`
    cannot use `ix_jobs_title_normalized`, and the rows being walked are mostly
    description text, so thirty of them read the table thirty times to look at
    a couple of megabytes of titles. Folded into one scan with a conditional
    sum per suggestion instead.
    """
    tallies = [
        func.sum(case((models.Job.title.ilike(f"%{title}%"), 1), else_=0)).label(f"t{index}")
        for index, title in enumerate(TITLE_SUGGESTIONS)
    ]
    with session_scope(config.db_path) as session:
        row = session.execute(
            select(*tallies).where(models.Job.is_active.is_(True))
        ).one()
    # An empty corpus sums to NULL rather than to zero, and a suggestion nobody
    # is hiring for has to read as zero, not as missing.
    return {title: int(value or 0) for title, value in zip(TITLE_SUGGESTIONS, row, strict=True)}


def _total_active(config: Config) -> int:
    with session_scope(config.db_path) as session:
        return session.execute(
            select(func.count(models.Job.id)).where(models.Job.is_active.is_(True))
        ).scalar_one()


def _generation(config: Config) -> tuple[int, int]:
    """A cheap stamp that changes whenever the counts would.

    `(highest id, active rows)`: the first moves when a job is inserted, the
    second when one is deactivated, and both come off indexes in a few
    milliseconds. SQLite's own `PRAGMA data_version` would be cheaper still,
    but it does not react to writes made by the same process — and a sync runs
    in this one — so it would go stale exactly when it matters.
    """
    with session_scope(config.db_path) as session:
        highest, active = session.execute(
            select(func.max(models.Job.id), func.count(models.Job.id)).where(
                models.Job.is_active.is_(True)
            )
        ).one()
    return (highest or 0, active)


# Keyed by database, because one process can serve more than one corpus and a
# count from the wrong one is worse than no cache. Guarded by a lock: requests
# are served from a threadpool and a run works the corpus from its own thread.
_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def build(config: Config) -> dict[str, Any]:
    """Everything the two autocompletes need, in one round trip.

    Cached against the corpus generation rather than a clock, so a finished
    sync shows up on the very next request instead of one expiry later.
    """
    key = str(config.db_path)
    generation = _generation(config)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == generation:
            return copy.deepcopy(cached[1])

    payload = _build(config)
    with _cache_lock:
        _cache[key] = (generation, payload)
    # Handed out by value. The entry is a plain dict of lists of dicts, so a
    # caller keeping the result and editing it would otherwise be editing what
    # every later request gets back. A tenth of a millisecond against that.
    return copy.deepcopy(payload)


def _build(config: Config) -> dict[str, Any]:
    countries = _country_counts(config)
    total = _total_active(config)

    locations: list[dict[str, Any]] = [
        {"value": "Worldwide", "label": "every market in the corpus", "count": total}
    ]
    for name, codes in regions.REGIONS.items():
        if name not in REGION_LABELS:
            continue
        locations.append(
            {
                "value": name.title(),
                "label": f"region · {len(codes)} countries",
                "count": sum(countries.get(code, 0) for code in codes),
            }
        )

    seen_codes: set[str] = set()
    for name, code in regions.COUNTRY_NAMES.items():
        if code in seen_codes:
            continue
        seen_codes.add(code)
        locations.append(
            {"value": name.title(), "label": code, "count": countries.get(code, 0)}
        )

    locations.sort(key=lambda row: row["count"], reverse=True)

    titles = [
        {"value": title, "label": "", "count": count}
        for title, count in sorted(_title_counts(config).items(), key=lambda kv: -kv[1])
    ]
    return {"titles": titles, "locations": locations, "active_jobs": total}
