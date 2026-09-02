"""Preferences to Upwork's search vocabulary.

Kept apart from the adapter and free of I/O for the same reason
`linkedin_query` is: a silent mistake here costs a whole run of wrong jobs,
and it is far cheaper to pin down as a pure function than through a fetch.

The one asymmetry worth naming: the API takes a single `budget_min`/`budget_max`
pair, but an hourly rate floor and a fixed-price floor are different numbers.
That is why a query becomes two refs rather than one.

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

from typing import Any

from jobhunt.preferences import UpworkPreferences

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


def search_params(query: str, job_type: str, prefs: UpworkPreferences) -> dict[str, Any]:
    """Build the find_jobs params for one (query, job_type) ref."""
    params: dict[str, Any] = {
        "query": query,
        "job_type": job_type,
        "sort": prefs.sort,
        "limit": PAGE_SIZE,
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

    return params


def refs_for(prefs: UpworkPreferences) -> list[tuple[str, str]]:
    """One (query, job_type) pair per query per job type asked for.

    A query becomes as many refs as job types because `budget_min` cannot
    carry both an hourly floor and a fixed floor in the same search call.
    """
    return [(query, job_type) for query in prefs.queries for job_type in prefs.job_types]
