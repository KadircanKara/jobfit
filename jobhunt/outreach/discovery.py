"""Where contacts come from, once anything can find them.

The stated source (the posting itself naming a hiring contact) lands here first.
The inferred source (searching for people at the company) is a separate, later
addition; the two are additive, never mutually exclusive.

The inferred search is deliberately not wired into anything that walks many
jobs. It is a per-company network call, and the fetch path already runs against
hundreds of jobs a cycle - reaching for this there would multiply that by every
job's company, which is slow and exactly the pattern LinkedIn flags an account
for. `search_company` is called from one place: the `find` HTTP endpoint,
which exists precisely so a person decides, per job, to spend that call.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any, Protocol

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Job, utcnow
from jobhunt.db.session import session_scope

# The roles that answer a job posting. Kept short: a wider net returns people who
# work at the company but have no say in this role, which is worse than nothing
# because it looks like a lead.
ROLE_KEYWORDS: tuple[str, ...] = (
    "recruiter",
    "talent acquisition",
    "technical recruiter",
    "engineering manager",
    "head of engineering",
)

CACHE_HOURS = 24
_CACHE_KEY = "linkedin_people_{company}"


@dataclasses.dataclass(frozen=True)
class ContactCandidate:
    full_name: str
    headline: str | None = None
    profile_url: str | None = None
    origin: str = "job_poster"


class SearchesPeople(Protocol):
    """The one method `search_company` needs. `UnipileClient` satisfies this."""

    def search_people(
        self, company: str, keywords: list[str], *, limit: int = 5
    ) -> list[dict[str, Any]]: ...


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


def _cache_key(company_name: str) -> str:
    # Cache identity is the company, not the exact string typed for it - "Acme"
    # and " acme " must not each pay for their own search.
    return _CACHE_KEY.format(company=company_name.strip().lower())


def _cached(config: Config, company_name: str, now: dt.datetime) -> list[ContactCandidate] | None:
    # A session of its own, committing independently of whatever request called
    # in: a cache read/write is a side effect of answering the question, not
    # part of the question's own transaction, and there is nothing here that
    # needs to roll back together with the caller's session.
    with session_scope(config.db_path) as session:
        raw = store.meta_get(session, _cache_key(company_name))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        stamp = dt.datetime.fromisoformat(payload["stamp"])
        items = payload["candidates"]
        candidates = [ContactCandidate(**item) for item in items]
    except (ValueError, KeyError, TypeError):
        # A cache entry that fails to parse - malformed JSON, a missing key, or
        # (since ContactCandidate(**item) lives in this same try) a stored
        # "candidates" value shaped wrong for that constructor - is not a
        # reason to raise out of a read path. Treat it the same as no cache
        # and search again.
        return None
    if now - stamp > dt.timedelta(hours=CACHE_HOURS):
        return None
    return candidates


def _store(
    config: Config, company_name: str, candidates: list[ContactCandidate], now: dt.datetime
) -> None:
    payload = {
        "stamp": now.isoformat(),
        "candidates": [dataclasses.asdict(c) for c in candidates],
    }
    with session_scope(config.db_path) as session:
        store.meta_set(session, _cache_key(company_name), json.dumps(payload))


def search_company(
    client: SearchesPeople,
    company_name: str,
    *,
    config: Config,
    now: dt.datetime | None = None,
    limit: int = 5,
) -> list[ContactCandidate]:
    """Recruiters and managers at this company. Inferred, never stated.

    On request only - see the module docstring for why nothing in a sync may
    call this. Cached for a day per company so reopening the same job's drawer
    does not spend a second search on an answer that has not changed.

    `client.search_people`'s response shape could not be verified against a
    live account (Task 10). A malformed top-level body already raises
    `UnipileError` in the client itself, which this function does not catch -
    that is the caller's decision (the `find` endpoint turns it into a 502
    without losing the stated contact). What this function does guard against
    is a well-formed list of items that are individually the wrong shape: a
    non-dict entry, or one missing "name", is skipped rather than raised, so
    one bad record degrades to "no candidate" for that record, not an
    exception for the whole search.
    """
    now = now or utcnow()
    cached = _cached(config, company_name, now)
    if cached is not None:
        return cached
    items = client.search_people(company_name, list(ROLE_KEYWORDS), limit=limit)
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        headline = item.get("headline")
        profile_url = item.get("profile_url")
        candidates.append(
            ContactCandidate(
                full_name=name.strip(),
                # A field that isn't a string (Unipile handing back a nested
                # object where a scalar was expected - exactly the shape
                # uncertainty this task exists to absorb) must not reach
                # `ContactCandidate` unchecked: `normalize_profile_url`
                # downstream calls `.strip()` on this value.
                headline=(headline if isinstance(headline, str) and headline.strip() else None),
                profile_url=(
                    profile_url if isinstance(profile_url, str) and profile_url.strip() else None
                ),
                origin="company_search",
            )
        )
    _store(config, company_name, candidates, now)
    return candidates
