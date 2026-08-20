"""Suggestions for the two tag fields, counted against the live corpus.

The counts are the point. Dropping a title silently deletes every job it would
have matched, and the CLI only reports that after the fact. Here the number
sits next to the suggestion before it is chosen.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

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
    counts: dict[str, int] = {}
    with session_scope(config.db_path) as session:
        for title in TITLE_SUGGESTIONS:
            counts[title] = session.execute(
                select(func.count(models.Job.id)).where(
                    models.Job.is_active.is_(True),
                    models.Job.title.ilike(f"%{title}%"),
                )
            ).scalar_one()
    return counts


def _total_active(config: Config) -> int:
    with session_scope(config.db_path) as session:
        return session.execute(
            select(func.count(models.Job.id)).where(models.Job.is_active.is_(True))
        ).scalar_one()


def build(config: Config) -> dict[str, Any]:
    """Everything the two autocompletes need, in one round trip."""
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
