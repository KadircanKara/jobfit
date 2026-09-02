"""The message, before a model ever writes one.

Phase 1 drafts from a template on purpose: the point of this phase is the route,
the approval, and the caps around them. A generated message would make every one
of those harder to test and would hide the thing being built.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from jobhunt.db.models import Contact, Job
from jobhunt.outreach import provider

if TYPE_CHECKING:
    from jobhunt.config import Config

# Fallback used when a caller has no config to hand (or omits it) - keeps the
# rate line's default in one place rather than duplicating it into DEFAULT_CONFIG.
DEFAULT_RATE_LINE = "$30/hour"

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


def _rate_line(config: Config | None) -> str:
    if config is None:
        return DEFAULT_RATE_LINE
    return config.get("outreach", "rate_line", default=DEFAULT_RATE_LINE)


def _truncated_title(title: str, budget: int) -> str:
    """Fit a title into `budget` characters, on a word boundary where possible.

    The 300-character invite note has to survive a title of any length - the
    ask ("would you be open to connecting?") is fixed, so the title is the only
    part with room to give.
    """
    if len(title) <= budget:
        return title
    if budget <= 1:
        return title[:budget]
    clipped = title[: budget - 1].rsplit(" ", 1)[0]
    if not clipped:
        clipped = title[: budget - 1]
    return clipped + "…"


def _upwork_note(job: Job, contact: Contact) -> str:
    prefix = f'Hi {_first_name(contact)} - I saw your "'
    suffix = '" post on Upwork and would love to connect about it.'
    room = NOTE_LIMIT - len(prefix) - len(suffix)
    return f"{prefix}{_truncated_title(job.title, room)}{suffix}"


def _upwork_pitch(job: Job, contact: Contact, config: Config | None) -> str:
    """The full Upwork opening, in the register the user actually sends.

    Project bullets are placeholders: wiring them to the CV (via
    `ranking.profile_summary` and the `master.tex`-derived summary) is a later
    change, not this one - hardcoding today's projects here would only go stale.
    """
    rate = _rate_line(config)
    rate_sentence = (
        f"I am available on short notice, and given my experience I believe a {rate} rate is fair.\n\n"
        if rate
        else "I am available on short notice.\n\n"
    )
    return (
        f"Hi {_first_name(contact)},\n\n"
        f'I hope everything is fine. I am writing to you for "{job.title}" that you shared on Upwork. '
        "I am the developer you are looking for. I want to provide you with the best and fastest "
        "service. If we can have a meeting at a convenient time, I would like to discuss your project "
        "in detail.\n\n"
        "I have finalized two projects that honed my skills in the qualifications you are looking "
        "for:\n\n"
        "- Built a WhatsApp chatbot with memory for frozen food truck drivers, using the company "
        "knowledge base and the Perplexity API to give event-specific recommendations.\n"
        "- Built a voice assistant system for an imaginary banking client that places automated "
        "calls, using FastAPI for the backend and Next.js for the frontend.\n\n"
        "So far I have used FastAPI for backend and Next.js for frontend. I also know Dash Plotly "
        "and Streamlit for simple UI and fast product shipment.\n\n"
        "I believe I am a strong fit: I have turned complex problems into robust algorithms before, "
        "shown in my IEEE conference publications and my master's thesis. I am new to the AI "
        "automation field but the projects above show I can already ship value in it.\n\n"
        f"{rate_sentence}"
        "Best regards,\n"
        "Kadircan KARA, M.Sc.\n"
        "GitHub: https://github.com/KadircanKara"
    )


def template(job: Job, contact: Contact, route: str | None, config: Config | None = None) -> str:
    """The starting message for this route. Edited freely afterwards.

    `config` is optional so the pre-existing non-Upwork callers stay unchanged;
    only the Upwork branch reads it, for the configurable rate line.
    """
    if job.source == "upwork":
        if route == provider.INVITE_NOTE:
            return _upwork_note(job, contact)
        return _upwork_pitch(job, contact, config)
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
