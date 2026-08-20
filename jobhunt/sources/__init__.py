"""Source adapters. Each one is isolated: a failure marks the run degraded, never fails it."""
from __future__ import annotations

from jobhunt.sources.ashby import AshbyAdapter
from jobhunt.sources.base import BoardRef, JobPosting, RateLimit, SourceAdapter
from jobhunt.sources.greenhouse import GreenhouseAdapter
from jobhunt.sources.lever import LeverAdapter
from jobhunt.sources.personio import PersonioAdapter
from jobhunt.sources.recruitee import RecruiteeAdapter
from jobhunt.sources.smartrecruiters import SmartRecruitersAdapter

REGISTRY: dict[str, type] = {
    GreenhouseAdapter.source_id: GreenhouseAdapter,
    AshbyAdapter.source_id: AshbyAdapter,
    LeverAdapter.source_id: LeverAdapter,
    RecruiteeAdapter.source_id: RecruiteeAdapter,
    SmartRecruitersAdapter.source_id: SmartRecruitersAdapter,
    PersonioAdapter.source_id: PersonioAdapter,
}

__all__ = [
    "REGISTRY",
    "AshbyAdapter",
    "BoardRef",
    "GreenhouseAdapter",
    "JobPosting",
    "LeverAdapter",
    "PersonioAdapter",
    "RecruiteeAdapter",
    "SmartRecruitersAdapter",
    "RateLimit",
    "SourceAdapter",
]


def get(source_id: str):
    """Return an adapter class by source id, or raise with the valid list."""
    try:
        return REGISTRY[source_id]
    except KeyError:
        raise KeyError(f"unknown source {source_id!r}. known: {', '.join(sorted(REGISTRY))}") from None
