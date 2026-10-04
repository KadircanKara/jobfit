"""Preferences to kariyer.net's search URL parameters.

Kept apart from the adapter and free of I/O, like linkedin_query.py: a wrong
code here silently fetches the wrong jobs for a whole run, and it is far
cheaper to pin down as a pure function.

kariyer.net has no public API. The search page (`/is-ilanlari`) is rendered on
the server, and every filter is a query parameter its own front end reads back
(`convertQueriesToFilters` in the site bundle). Probed live 2026-10-04:

    kw=<text>           keyword
    wm=0,1,2            work model: 0 on-site, 1 remote, 2 hybrid
    tpst=1,2,4,5        work type: 1 full time, 2 seasonal / project based,
                        4 part time, 5 freelance
    lpst=1..11          position level (see _LEVEL)
    date=1g|3g|7g|15g   posted within: today, 3, 7, 15 days (also 3s, 8s hours)
    ct=<city id>        city; 998 is all of Istanbul (both sides)
    cp=<n>              page, about 50 cards each

robots.txt disallows `/filtre` and `?ckw`, so neither is ever built here.
"""
from __future__ import annotations

from jobhunt.preferences import Preferences
from jobhunt.rank.deterministic import SENIORITY_ORDER

SEARCH_URL = "https://www.kariyer.net/is-ilanlari"
BASE_URL = "https://www.kariyer.net"

_WORK_MODEL = {"onsite": "0", "remote": "1", "hybrid": "2"}

# kariyer.net has one bucket for seasonal and project-based work and a separate
# one for freelance; both of ours that mean "not permanent" ask for both.
# Internship is not a work type there but a position level, see _LEVEL.
_WORK_TYPE = {
    "full_time": ["1"],
    "part_time": ["4"],
    "contract": ["2", "5"],
    "temporary": ["2"],
}

# Position levels, against our seniority ladder. kariyer.net's ladder mixes
# individual and management rungs; the mapping keeps a band wide rather than
# guessing a precise rung that the employer may not have picked:
#   8 Stajyer (intern)          5 Yeni Başlayan (new starter)
#   9 Uzman Yardımcısı (assistant specialist)
#   4 Uzman (specialist)        3 Yönetici adayı (manager candidate)
#   2 Orta düzey yönetici (middle manager)   1 Üst düzey yönetici (executive)
# Left out: 6 freelancer, 7 blue collar, 10 staff, 11 service personnel.
_LEVEL = {
    "intern": ["8"],
    "junior": ["5", "9"],
    "mid": ["9", "4"],
    "senior": ["4"],
    "staff": ["4"],
    "lead": ["3", "2"],
    "principal": ["4", "2"],
}

# The age buckets the site offers, in days. A preference between two buckets
# rounds up to the wider one, never down: stage 1 trims the extra days, but a
# job the search never returned cannot be recovered.
_DATE_BUCKETS = ((1, "1g"), (3, "3g"), (7, "7g"), (15, "15g"))

# Cities worth naming, by how people type them. Anything else (another country,
# "remote") adds no city filter: the search is Turkey-wide already.
CITY_IDS = {
    "istanbul": "998",
    "ankara": "6",
    "izmir": "35",
    "bursa": "16",
    "antalya": "7",
    "kocaeli": "41",
    "eskisehir": "26",
    "adana": "1",
    "konya": "42",
    "kayseri": "38",
}

_TURKISH_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")


def fold(text: str) -> str:
    """Lower-case ASCII. `str.lower` turns "İ" into "i" plus a combining dot,
    so "İş Yerinde".lower() never equals "iş yerinde"."""
    return text.translate(_TURKISH_FOLD).strip().lower()


def city_ids(locations: list[str]) -> list[str]:
    """kariyer.net city ids for the Turkish cities among the locations."""
    ids: list[str] = []
    for location in locations:
        # "Istanbul, Turkey" and "İstanbul" both name the city.
        head = fold(location.split(",")[0])
        code = CITY_IDS.get(head)
        if code and code not in ids:
            ids.append(code)
    return ids


def _levels(prefs: Preferences) -> list[str]:
    if not prefs.experience_min and not prefs.experience_max:
        return []
    low = prefs.experience_min or SENIORITY_ORDER[0]
    high = prefs.experience_max or SENIORITY_ORDER[-1]
    if low not in SENIORITY_ORDER or high not in SENIORITY_ORDER:
        return []
    span = SENIORITY_ORDER[SENIORITY_ORDER.index(low) : SENIORITY_ORDER.index(high) + 1]
    codes: list[str] = []
    for level in span:
        for code in _LEVEL.get(level, []):
            if code not in codes:
                codes.append(code)
    return codes


def _date(max_age_days: int | None) -> str | None:
    if not max_age_days:
        return None
    for days, code in _DATE_BUCKETS:
        if max_age_days <= days:
            return code
    # Older than the widest bucket: no filter, and stage 1 applies the age.
    return None


def search_params(
    title: str,
    prefs: Preferences,
    *,
    cities: list[str] | None = None,
    page: int = 1,
) -> dict[str, str]:
    """Query params for one page of one title's search."""
    params: dict[str, str] = {"kw": title}

    # All three ticked filters nothing, so it is not sent.
    models = sorted({_WORK_MODEL[m] for m in prefs.work_model if m in _WORK_MODEL})
    if models and len(models) < len(_WORK_MODEL):
        params["wm"] = ",".join(models)

    types = sorted({code for t in prefs.job_types for code in _WORK_TYPE.get(t, [])})
    if types:
        params["tpst"] = ",".join(types)

    levels = _levels(prefs)
    # A band that also wants interns must not drop internships posted under the
    # intern level; "internship" as a job type asks for that level too.
    if "internship" in prefs.job_types and levels and "8" not in levels:
        levels.append("8")
    if levels:
        params["lpst"] = ",".join(levels)

    date = _date(prefs.max_age_days)
    if date:
        params["date"] = date

    if cities:
        params["ct"] = ",".join(cities)

    if page > 1:
        params["cp"] = str(page)
    return params
