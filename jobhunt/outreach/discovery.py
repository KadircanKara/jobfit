"""Where contacts come from, once anything can find them.

Phase 1 finds nothing: the LinkedIn source that would carry a job poster does not
exist yet (PLAN.md phase 6). The interface exists now so that landing it later is
one file, not a change to the API and the drawer as well. Until then, contacts
arrive by hand.
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
    """People named by the posting itself. Empty until LinkedIn ingestion lands."""
    return []
