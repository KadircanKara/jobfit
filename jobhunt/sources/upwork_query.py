"""Preferences to Upwork's search vocabulary.

Kept apart from the adapter and free of I/O for the same reason
`linkedin_query` is: a silent mistake here costs a whole run of wrong jobs,
and it is far cheaper to pin down as a pure function than through a fetch.

The one asymmetry worth naming: the API takes a single `budget_min`/`budget_max`
pair, but an hourly rate floor and a fixed-price floor are different numbers.
That is why a query becomes one ref per job type, and one per client location on top,
since `location` also takes a single value.

Facts below were verified live against the Upwork MCP, not re-derived from
documentation:
- `limit` maxes at 10. Sending more is silently ignored.
- There is no date filter of any kind on the signed-in search. That once
  justified pinning `sort` to "recency" and stopping pagination at an age
  cutoff, but `_check_age` drops old postings at rank time regardless, so the
  pin bought a marginally cheaper fetch and cost the whole relevance signal.
  `sort` is a preference now, defaulting to "relevance" - the website's own
  "Best match".
- `rate_min` (hourly) and `budget_min` (fixed) are different parameters, not
  synonyms.
- `experience_level` and `workload` each take a single string, not a list.
  When the user has selected more than one, the parameter is omitted entirely
  and the local filter handles the rest - a comma-joined list would be
  silently wrong rather than loudly rejected.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from jobhunt.preferences import UPWORK_FEED_QUERY, UPWORK_JOB_TYPES, UpworkPreferences

ORG_UID_ENV = "UPWORK_ORG_UID"

PAGE_SIZE = 10  # the API's hard maximum; anything higher is silently ignored


def _floor(job_type: str, prefs: UpworkPreferences) -> tuple[str, float] | None:
    """(parameter name, value) for this ref's contract type, or None.

    `rate_min` and `budget_min` are different parameters, not two names for
    one: `rate_min` is the hourly rate floor and `budget_min` is the
    fixed-price budget floor. Both were being sent as `budget_min`, which
    filtered hourly searches on the wrong field - confirmed live, the two
    return different result sets for the same query.

    Each is still its own ref, because the API takes one of each per call and
    a single search cannot carry both an hourly and a fixed floor.
    """
    if job_type == "hourly":
        name, floor = "rate_min", prefs.min_hourly
    else:
        name, floor = "budget_min", prefs.min_fixed
    # A floor of None or zero must be omitted rather than sent as zero, which
    # would be a meaningful and wrong filter.
    return (name, floor) if floor else None


@dataclasses.dataclass(frozen=True)
class UpworkRef:
    """One Upwork fetch: a keyword search or the Most Recent feed, for one job
    type and at most one client location."""

    query: str
    job_type: str
    location: str | None = None

    @property
    def is_feed(self) -> bool:
        return self.query == UPWORK_FEED_QUERY

    @property
    def token(self) -> str:
        """Self-describing on purpose: `sync` rebuilds a ref from a saved raw
        envelope with nothing but this string, so it has to carry everything."""
        parts = [self.query, self.job_type]
        if self.location:
            parts.append(self.location)
        return "|".join(parts)


def parse_ref(token: str) -> UpworkRef:
    """The inverse of `UpworkRef.token`, and the one place a token is split.

    The job type is found rather than counted to: a query may itself contain a
    "|", so the last segment naming a job type is the divider. A token written
    before locations existed simply has nothing after it.
    """
    parts = token.split("|")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] in UPWORK_JOB_TYPES:
            location = "|".join(parts[index + 1:]) or None
            return UpworkRef("|".join(parts[:index]), parts[index], location)
    # No job type anywhere: read it the way `partition` always did.
    query, _, job_type = token.partition("|")
    return UpworkRef(query, job_type)


def refs_for(prefs: UpworkPreferences) -> list[UpworkRef]:
    """Every fetch one run makes: the feed first, then each keyword search.

    One ref per job type, because `rate_min` and `budget_min` cannot share a
    call, and one per client location, because `location` takes one value. The
    feed goes first so a daily budget that runs out mid-run refuses keyword
    searches rather than the one search that honours a date.
    """
    queries = ([UPWORK_FEED_QUERY] if prefs.recommended_feed else []) + list(prefs.queries)
    locations: list[str | None] = list(prefs.client_locations) or [None]
    return [
        UpworkRef(q, job_type, location)
        for q in queries
        for job_type in prefs.job_types
        for location in locations
    ]


def _filters(job_type: str, prefs: UpworkPreferences, location: str | None) -> dict[str, Any]:
    """The filters a keyword search and the feed take alike."""
    params: dict[str, Any] = {
        "job_type": job_type,
        "verified_payment_only": prefs.verified_payment_only,
    }

    floor = _floor(job_type, prefs)
    if floor is not None:
        params[floor[0]] = floor[1]

    if len(prefs.experience_level) == 1:
        params["experience_level"] = prefs.experience_level[0]

    if len(prefs.workload) == 1:
        params["workload"] = prefs.workload[0]

    if prefs.proposals_max is not None:
        params["proposals_max"] = prefs.proposals_max

    if prefs.client_min_hires is not None:
        params["client_hires_min"] = prefs.client_min_hires

    if prefs.client_max_hires is not None:
        params["client_hires_max"] = prefs.client_max_hires

    if location:
        params["location"] = location

    return params


def search_params(
    query: str, job_type: str, prefs: UpworkPreferences, *, location: str | None = None
) -> dict[str, Any]:
    """Build the find_jobs `search` params for one keyword search."""
    return {
        "query": query,
        "sort": prefs.sort,
        "limit": PAGE_SIZE,
        **_filters(job_type, prefs, location),
    }


def feed_params(
    job_type: str, prefs: UpworkPreferences, max_age_days: int, *, location: str | None = None
) -> dict[str, Any]:
    """Build the find_jobs `smart_search` params for one Most Recent feed read.

    No `query` and no `sort`: the feed is Upwork's recommender, matched to the
    profile and newest first. `days_posted` is the only date filter Upwork
    honours anywhere, so the shared age limit is pushed down here as well as
    enforced at rank.
    """
    return {
        "mode": "most_recent",
        "limit": PAGE_SIZE,
        "days_posted": max(1, int(max_age_days)),
        **_filters(job_type, prefs, location),
    }
