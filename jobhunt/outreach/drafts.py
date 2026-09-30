"""The message, before a model ever writes one.

Phase 1 drafts from a template on purpose: the point of this phase is the route,
the approval, and the caps around them. A generated message would make every one
of those harder to test and would hide the thing being built.
"""
from __future__ import annotations

from jobhunt.db.models import Contact, Job
from jobhunt.outreach import provider

# LinkedIn's own ceiling on an invitation note. The long limit is well inside
# what a DM or InMail accepts; it exists to catch a runaway paste, not to police
# the writing.
NOTE_LIMIT = 300
LONG_LIMIT = 1900

LIMITS: dict[str, int] = {
    provider.DM: LONG_LIMIT,
    provider.FREE_INMAIL: LONG_LIMIT,
    provider.PAID_INMAIL: LONG_LIMIT,
    provider.INVITE_THEN_DM: LONG_LIMIT,
    provider.INVITE_NOTE: NOTE_LIMIT,
}


def limit_for(route: str | None) -> int:
    """Characters this route accepts. An unchosen route gets the long limit."""
    return LIMITS.get(route or provider.DM, LONG_LIMIT)


def over_limit(body: str, route: str | None) -> bool:
    return len(body) > limit_for(route)


def _first_name(contact: Contact) -> str:
    return (contact.full_name or "").split(" ")[0] or "there"


def _company(job: Job) -> str:
    company = getattr(job, "company", None)
    return getattr(company, "name", None) or "your team"


def template(job: Job, contact: Contact, route: str | None) -> str:
    """The starting message for this route. Edited freely afterwards."""
    if route == provider.INVITE_NOTE:
        return (
            f"Hi {_first_name(contact)} - I saw the {job.title} opening at {_company(job)} "
            "and it lines up closely with the backend work I have been doing. "
            "Would you be open to connecting?"
        )
    return (
        f"Hi {_first_name(contact)},\n\n"
        f"I came across the {job.title} role at {_company(job)} and it reads like the work I have "
        "spent the last few years on - high-throughput backend services, and the operational side "
        "of keeping them honest.\n\n"
        "I have applied through the posting. If it is useful I am happy to send a short summary of "
        "the closest project. Either way, thanks for putting the role out there.\n\n"
        "Kadircan"
    )
