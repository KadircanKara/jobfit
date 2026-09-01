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
- There is no date filter of any kind on the signed-in search. `sort` set to
  "recency" plus a client-side cutoff on each result's `created_date` is the
  only substitute - which is why SORT is a constant here rather than a
  preference the user could turn off.
- `experience_level` and `workload` each take a single string, not a list.
  When the user has selected more than one, the parameter is omitted entirely
  and the local filter handles the rest - a comma-joined list would be
  silently wrong rather than loudly rejected.
"""
from __future__ import annotations

from jobhunt.preferences import UpworkPreferences

ORG_UID_ENV = "UPWORK_ORG_UID"

PAGE_SIZE = 10  # the API's hard maximum; anything higher is silently ignored
SORT = "recency"  # there is no date filter; this plus a client-side cutoff on
# created_date is the substitute


def _budget_min(job_type: str, prefs: UpworkPreferences) -> float | None:
    """The one floor that applies to this ref's contract type.

    `budget_min`/`budget_max` is a single pair on the API, not one per
    contract type - which is why an hourly ref and a fixed ref are separate
    refs rather than one query carrying both floors.
    """
    floor = prefs.min_hourly if job_type == "hourly" else prefs.min_fixed
    # A floor of None or zero must be omitted rather than sent as
    # budget_min=0, which would be a meaningful and wrong filter.
    return floor if floor else None


def search_params(query: str, job_type: str, prefs: UpworkPreferences) -> dict[str, object]:
    """Build the find_jobs params for one (query, job_type) ref."""
    params: dict[str, object] = {
        "query": query,
        "job_type": job_type,
        "sort": SORT,
        "limit": PAGE_SIZE,
        "verified_payment_only": prefs.verified_payment_only,
    }

    budget_min = _budget_min(job_type, prefs)
    if budget_min is not None:
        params["budget_min"] = budget_min

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
