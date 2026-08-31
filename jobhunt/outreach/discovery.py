"""Where contacts come from, once anything can find them.

The stated source (the posting itself naming a hiring contact) lands here first.
An inferred source (searching for people at the company) is a separate later
addition; the two are additive, never mutually exclusive.
"""
from __future__ import annotations

import dataclasses

from jobhunt.db.models import Job


@dataclasses.dataclass(frozen=True)
class ContactCandidate:
    full_name: str
    headline: str | None = None
    profile_url: str | None = None
    origin: str = "job_poster"


def find_contacts(job: Job | None) -> list[ContactCandidate]:
    """People the posting itself names. Inferred contacts come from the drawer."""
    if job is None or not job.poster_name:
        return []
    return [
        ContactCandidate(
            full_name=job.poster_name,
            profile_url=job.poster_profile_url,
            origin="job_poster",
        )
    ]
