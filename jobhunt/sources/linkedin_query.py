"""Preferences to LinkedIn's guest-search parameter alphabet.

Kept apart from the adapter and free of I/O: this mapping is where a silent
mistake costs a whole run of wrong jobs, and it is far cheaper to pin down as a
pure function than through a fetch.
"""
from __future__ import annotations

from jobhunt.preferences import Preferences
from jobhunt.rank.deterministic import SENIORITY_ORDER

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# LinkedIn's experience ladder is shorter than ours. Two of our levels share a
# rung rather than inventing precision LinkedIn does not offer.
_EXPERIENCE = {
    "intern": "1",
    "junior": "2",
    "mid": "3",
    "senior": "4",
    "staff": "4",
    "lead": "5",
    "principal": "5",
}

_WORK_TYPE = {"onsite": "1", "on-site": "1", "remote": "2", "hybrid": "3"}

_JOB_TYPE = {
    "full_time": "F", "full-time": "F", "fulltime": "F",
    "part_time": "P", "part-time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
    "volunteer": "V",
    "other": "O",
}

# Only places worth naming. An unlisted one falls back to the text field, which
# LinkedIn resolves itself - less precise, never wrong.
GEO_IDS: dict[str, str] = {
    "worldwide": "92000000",
    "european union": "91000000",
    "europe": "100506914",
    "germany": "101282230",
    "netherlands": "102890719",
    "united kingdom": "101165590",
    "turkey": "102105699",
    "türkiye": "102105699",
    "united states": "103644278",
}


def _band(prefs: Preferences) -> list[str]:
    """The rungs between the floor and ceiling the search asked for.

    A one-sided band still resolves to a real range: min alone means "that
    level and up", max alone means "up to that level" - so only the absence of
    both sides skips the filter, never the absence of one.
    """
    if not prefs.experience_min and not prefs.experience_max:
        return []
    low = prefs.experience_min or SENIORITY_ORDER[0]
    high = prefs.experience_max or SENIORITY_ORDER[-1]
    if low not in SENIORITY_ORDER or high not in SENIORITY_ORDER:
        return []
    span = SENIORITY_ORDER[SENIORITY_ORDER.index(low) : SENIORITY_ORDER.index(high) + 1]
    # Two of our levels collapse onto one LinkedIn rung (senior/staff -> 4,
    # lead/principal -> 5). Deduplicated while preserving first-seen order, so
    # a junior-to-mid band reads "2,3" and never the codes out of sequence.
    seen: list[str] = []
    for level in span:
        code = _EXPERIENCE.get(level)
        if code and code not in seen:
            seen.append(code)
    return seen


def search_params(
    title: str,
    location: str | None,
    prefs: Preferences,
    *,
    start: int = 0,
) -> dict[str, str]:
    """Build the guest-search query params for one page of one title."""
    params: dict[str, str] = {"keywords": title, "start": str(start)}

    if location:
        geo = GEO_IDS.get(location.strip().lower())
        if geo:
            params["geoId"] = geo
        else:
            # Unknown to our table, not to LinkedIn: never drop it silently.
            params["location"] = location

    band = _band(prefs)
    if band:
        params["f_E"] = ",".join(band)

    work = [_WORK_TYPE[m.lower()] for m in prefs.work_model if m.lower() in _WORK_TYPE]
    if work:
        params["f_WT"] = ",".join(sorted(set(work)))

    types = [_JOB_TYPE[t.lower()] for t in prefs.job_types if t.lower() in _JOB_TYPE]
    if types:
        params["f_JT"] = ",".join(sorted(set(types)))

    if prefs.max_age_days:
        params["f_TPR"] = f"r{int(prefs.max_age_days) * 86400}"

    return params
