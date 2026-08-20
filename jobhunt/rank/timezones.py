"""Working-hours overlap between a job's location and the user's timezone.

PLAN.md section 7 calls this out as mattering more than it looks, and it does:
GMT+3 rules out a large share of US-anchored remote listings, and catching that
deterministically is the difference between a cheap run and an expensive one.

A country is mapped to one representative UTC offset. That is deliberately
approximate. The US spans five zones, but every one of them fails a 4 hour
overlap test against Istanbul, so the approximation never changes an outcome.
Where a country genuinely straddles a decision boundary, the offset chosen is
the one closest to Europe, which errs toward keeping a job rather than dropping
it. A job wrongly kept costs one LLM call; a job wrongly dropped is never seen.
"""
from __future__ import annotations

# ISO 3166-1 alpha-2 -> representative UTC offset in hours.
COUNTRY_OFFSETS: dict[str, float] = {
    # Europe
    "GB": 0, "IE": 0, "PT": 0, "IS": 0,
    "FR": 1, "DE": 1, "NL": 1, "BE": 1, "ES": 1, "IT": 1, "CH": 1, "AT": 1,
    "SE": 1, "NO": 1, "DK": 1, "PL": 1, "CZ": 1, "HU": 1, "SK": 1, "SI": 1,
    "HR": 1, "RS": 1, "BA": 1, "AL": 1, "MK": 1, "LU": 1, "MT": 1, "ME": 1,
    "FI": 2, "EE": 2, "LV": 2, "LT": 2, "RO": 2, "BG": 2, "GR": 2, "CY": 2,
    "UA": 2, "MD": 2,
    "TR": 3, "RU": 3, "BY": 3, "GE": 4, "AM": 4, "AZ": 4,
    # Middle East and Africa
    "IL": 2, "EG": 2, "ZA": 2, "ZW": 2, "ZM": 2,
    "SA": 3, "AE": 4, "QA": 3, "KW": 3, "BH": 3, "OM": 4, "JO": 3, "LB": 3, "IQ": 3,
    "KE": 3, "TZ": 3, "UG": 3, "ET": 3,
    "NG": 1, "GH": 0, "MA": 1, "TN": 1, "DZ": 1, "SN": 0, "CI": 0, "CM": 1,
    # Asia
    "PK": 5, "IN": 5.5, "LK": 5.5, "BD": 6, "NP": 5.75,
    "TH": 7, "VN": 7, "ID": 7, "MY": 8, "SG": 8, "PH": 8, "CN": 8, "HK": 8, "TW": 8,
    "JP": 9, "KR": 9, "KZ": 6, "UZ": 5,
    # Oceania
    "AU": 10, "NZ": 12,
    # Americas
    "BR": -3, "AR": -3, "UY": -3, "CL": -4, "PY": -4, "BO": -4, "VE": -4,
    "CO": -5, "PE": -5, "EC": -5, "PA": -5,
    "US": -6, "CA": -6, "MX": -6, "CR": -6, "GT": -6, "SV": -6, "HN": -6,
    "DO": -4, "PR": -4, "JM": -5, "CU": -5,
}

# Anything a source writes when it means "no location restriction at all".
WORLDWIDE_MARKERS = (
    "worldwide", "anywhere", "global", "remote - global", "any location",
    "location independent", "fully remote", "work from anywhere",
)

# A conventional working day, local time, at both ends.
WORKDAY_START = 9
WORKDAY_END = 18
FULL_OVERLAP = float(WORKDAY_END - WORKDAY_START)


def is_worldwide(location_raw: str | None) -> bool:
    if not location_raw:
        return False
    lowered = location_raw.lower()
    return any(marker in lowered for marker in WORLDWIDE_MARKERS)


def offset_for(country: str | None) -> float | None:
    if not country:
        return None
    return COUNTRY_OFFSETS.get(country.strip().upper()[:2])


def overlap_hours(job_country: str | None, base_offset: float = 3.0) -> float | None:
    """Overlapping working hours between the job's country and the user's.

    Returns None when the country is unknown, which callers must treat as "no
    objection" rather than as zero. Dropping every job with an unparsed location
    would silently discard most aggregator postings.
    """
    offset = offset_for(job_country)
    if offset is None:
        return None
    return _overlap(offset, base_offset)


def _overlap(job_offset: float, base_offset: float) -> float:
    job_start = WORKDAY_START - job_offset
    job_end = WORKDAY_END - job_offset
    base_start = WORKDAY_START - base_offset
    base_end = WORKDAY_END - base_offset
    # Both windows are in UTC now, but a day wraps, so compare against the base
    # window shifted by a day in each direction and take the best overlap.
    best = 0.0
    for shift in (-24.0, 0.0, 24.0):
        low = max(job_start + shift, base_start)
        high = min(job_end + shift, base_end)
        best = max(best, high - low)
    return max(0.0, best)


def base_offset_for(timezone_name: str | None) -> float:
    """Offset for a configured base timezone name, defaulting to Istanbul.

    Only the handful of names a user might realistically set are mapped. An
    unknown name falls back to GMT+3 rather than raising, because a filter
    config typo must not take the whole ranking pass down.
    """
    known = {
        "europe/istanbul": 3.0, "europe/london": 0.0, "europe/berlin": 1.0,
        "europe/paris": 1.0, "europe/amsterdam": 1.0, "europe/madrid": 1.0,
        "europe/kyiv": 2.0, "europe/athens": 2.0, "utc": 0.0,
        "america/new_york": -5.0, "america/los_angeles": -8.0,
    }
    return known.get((timezone_name or "").strip().lower(), 3.0)
