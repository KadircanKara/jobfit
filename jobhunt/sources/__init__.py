"""Source adapters. Each one is isolated: a failure marks the run degraded, never fails it."""
from __future__ import annotations

from jobhunt.sources.arbeitnow import ArbeitnowAdapter
from jobhunt.sources.ashby import AshbyAdapter
from jobhunt.sources.base import BoardRef, JobPosting, RateLimit, SourceAdapter
from jobhunt.sources.careerjet import CareerjetAdapter
from jobhunt.sources.greenhouse import GreenhouseAdapter
from jobhunt.sources.jobicy import JobicyAdapter
from jobhunt.sources.lever import LeverAdapter
from jobhunt.sources.linkedin import LinkedInAdapter
from jobhunt.sources.personio import PersonioAdapter
from jobhunt.sources.recruitee import RecruiteeAdapter
from jobhunt.sources.remoteok import RemoteOkAdapter
from jobhunt.sources.remotive import RemotiveAdapter
from jobhunt.sources.smartrecruiters import SmartRecruitersAdapter
from jobhunt.sources.techcareer import TechcareerAdapter
from jobhunt.sources.workable import WorkableAdapter
from jobhunt.sources.workable_search import WorkableSearchAdapter
from jobhunt.sources.wwr import WeWorkRemotelyAdapter

REGISTRY: dict[str, type] = {
    GreenhouseAdapter.source_id: GreenhouseAdapter,
    AshbyAdapter.source_id: AshbyAdapter,
    LeverAdapter.source_id: LeverAdapter,
    RecruiteeAdapter.source_id: RecruiteeAdapter,
    SmartRecruitersAdapter.source_id: SmartRecruitersAdapter,
    PersonioAdapter.source_id: PersonioAdapter,
    RemotiveAdapter.source_id: RemotiveAdapter,
    RemoteOkAdapter.source_id: RemoteOkAdapter,
    ArbeitnowAdapter.source_id: ArbeitnowAdapter,
    JobicyAdapter.source_id: JobicyAdapter,
    WeWorkRemotelyAdapter.source_id: WeWorkRemotelyAdapter,
    TechcareerAdapter.source_id: TechcareerAdapter,
    WorkableAdapter.source_id: WorkableAdapter,
    WorkableSearchAdapter.source_id: WorkableSearchAdapter,
    LinkedInAdapter.source_id: LinkedInAdapter,
    CareerjetAdapter.source_id: CareerjetAdapter,
}

__all__ = [
    "REGISTRY",
    "ArbeitnowAdapter",
    "AshbyAdapter",
    "BoardRef",
    "CareerjetAdapter",
    "GreenhouseAdapter",
    "JobPosting",
    "JobicyAdapter",
    "LeverAdapter",
    "LinkedInAdapter",
    "PersonioAdapter",
    "RecruiteeAdapter",
    "RemoteOkAdapter",
    "RemotiveAdapter",
    "SmartRecruitersAdapter",
    "RateLimit",
    "SourceAdapter",
    "TechcareerAdapter",
    "WeWorkRemotelyAdapter",
    "WorkableAdapter",
    "WorkableSearchAdapter",
]


def get(source_id: str):
    """Return an adapter class by source id, or raise with the valid list."""
    try:
        return REGISTRY[source_id]
    except KeyError:
        raise KeyError(f"unknown source {source_id!r}. known: {', '.join(sorted(REGISTRY))}") from None
