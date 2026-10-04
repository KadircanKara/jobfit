"""Careerjet's job search API, Turkish locale.

Careerjet aggregates Turkish boards (and employer sites) behind one JSON API,
with no Cloudflare in front of it. Probed live 2026-10-04:

    GET https://search.api.careerjet.net/v4/query
        ?locale_code=tr_TR&keywords=<title>&location=<city>
        &sort=date&page=<n>&page_size=<1..100>
        &user_ip=<ip>&user_agent=<ua>
        Authorization: Basic base64("<api key>:")
        -> {"type": "JOBS", "hits": 62, "pages": 4, "jobs": [...]}

The key is free from a Careerjet publisher account and is read from the
environment only (CAREERJET_API_KEY), never from a config file. The keyless
legacy API (public.api.careerjet.net) now answers "only accessible for
authenticated legacy users", so it is not used.

`user_ip` and `user_agent` are required: the IP and browser of whoever caused
the call. For a personal tool that is the user, so CAREERJET_USER_IP may name
their address; the User-Agent is this tool's own.

A location Careerjet cannot pin down does not fail: it answers
`{"type": "LOCATIONS", ...}` with no jobs, which normalizes to nothing.

What a job carries: title, company, locations, date, a short excerpt with the
keywords in <b>, salary fields when stated, and a `url` through Careerjet's
click tracker. There is no job id, and the tracker URL differs on every call
for the same job, so the identity is a hash of title, company and location.
The excerpt is a snippet; the posting's full text is on the site it came from.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt import preferences as preferences_module
from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

SEARCH_URL = "https://search.api.careerjet.net/v4/query"
LOCALE = "tr_TR"
KEY_ENV = "CAREERJET_API_KEY"
USER_IP_ENV = "CAREERJET_USER_IP"

# Our job types against Careerjet's two filters. Only one value can be sent
# per filter, so a preference naming several types sends neither.
_WORK_HOURS = {"full_time": "f", "part_time": "p"}
_CONTRACT = {"contract": "c", "temporary": "t", "internship": "i"}

_SALARY_PERIOD = {"Y": "annual", "M": "monthly", "W": "weekly", "D": "daily", "H": "hourly"}

# Cities worth sending as `location`. Anything else - another country,
# "Remote", "Europe" - searches all of Turkey, which tr_TR already is.
_TR_CITIES = frozenset({
    "istanbul", "ankara", "izmir", "bursa", "antalya", "kocaeli", "eskisehir",
    "adana", "konya", "kayseri", "gaziantep", "mersin", "sakarya", "tekirdag",
    "denizli", "samsun", "trabzon", "manisa", "mugla", "edirne",
})
_COUNTRY_WORDS = frozenset({"turkey", "turkiye", "tr"})
_TURKISH_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")

_REMOTE = re.compile(r"\b(uzaktan|remote|evden)\b", re.I)
_HYBRID = re.compile(r"\b(hibrit|hybrid)\b", re.I)


class MissingApiKey(RuntimeError):
    """CAREERJET_API_KEY is unset. Raised per fetch, so the run reports it."""


def _fold(text: str) -> str:
    return text.translate(_TURKISH_FOLD).strip().lower()


def search_location(location: str) -> str:
    """The `location` to send for one preferred location: a city, or ""."""
    folded = _fold(location)
    first = folded.split(",")[0].strip()
    return location.split(",")[0].strip() if first in _TR_CITIES else ""


class CareerjetAdapter(HttpAdapter):
    source_id = "careerjet"
    market = "tr_local"
    rate_limit = RateLimit(2.0)
    # Searches built from preferences every run. See SourceAdapter.generates_refs.
    generates_refs = True
    # Newest first, 100 a page. Two pages is the last few weeks of a common
    # title in a big city; past that the jobs are older than max_age_days.
    PAGE_SIZE = 100
    MAX_PAGES = 2

    def __init__(self, refs: list[BoardRef] | None = None) -> None:
        super().__init__(refs)
        # Pages already collected for a search a 429 interrupted. See
        # WorkableSearchAdapter._resume: same contract, same reason.
        self._resume: dict[str, list[Any]] = {}

    def still_fetching(self) -> bool:
        """Without a key every fetch fails at once; pacing between them is waste."""
        return bool(os.environ.get(KEY_ENV, "").strip())

    def board_refs(self, prefs: preferences_module.Preferences) -> list[BoardRef]:
        """One search per title and Turkish city; a non-Turkish place searches all of Turkey."""
        locations = sorted({search_location(loc) for loc in prefs.locations} or {""})
        types = {t.lower().replace("-", "_") for t in prefs.job_types}
        extra: dict[str, str] = {}
        if len(types) == 1:
            (only,) = types
            if only in _WORK_HOURS:
                extra["work_hours"] = _WORK_HOURS[only]
            elif only in _CONTRACT:
                extra["contract_type"] = _CONTRACT[only]
        return [
            BoardRef(
                provider=self.source_id,
                token=f"{title}|{location}",
                market=self.market,
                extra=dict(extra),
            )
            for title in dict.fromkeys(prefs.titles)
            for location in locations
        ]

    def params(self, ref: BoardRef, page: int, user_agent: str) -> dict[str, str]:
        title, _, location = ref.token.partition("|")
        params = {
            "locale_code": LOCALE,
            "keywords": title,
            "sort": "date",
            "page": str(page),
            "page_size": str(self.PAGE_SIZE),
            "user_ip": os.environ.get(USER_IP_ENV, "").strip() or "127.0.0.1",
            "user_agent": user_agent,
        }
        if location:
            params["location"] = location
        for key in ("work_hours", "contract_type"):
            if ref.extra.get(key):
                params[key] = ref.extra[key]
        return params

    # --- fetch ------------------------------------------------------------

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """Every page of one search, up to MAX_PAGES."""
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            raise MissingApiKey(
                f"{KEY_ENV} is unset. Get a free key from a Careerjet publisher "
                "account (careerjet.com/partners) and put it in .env."
            )
        user_agent = client.headers.get("User-Agent", "jobhunt")
        pages = self._resume.pop(ref.token, [])
        while len(pages) < self.MAX_PAGES:
            if pages:
                self.rate_limit.sleep()
            response = client.get(
                SEARCH_URL,
                params=self.params(ref, len(pages) + 1, user_agent),
                auth=(key, ""),
            )
            if response.status_code == 429:
                self._resume[ref.token] = pages
            response.raise_for_status()
            page = response.json()
            pages.append(page)
            if not isinstance(page, dict) or page.get("type") != "JOBS":
                break
            if len(pages) >= int(page.get("pages") or 0) or not page.get("jobs"):
                break
        return {"pages": pages}

    # --- normalize ----------------------------------------------------------

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, dict):
            return
        seen: set[str] = set()
        for page in raw.get("pages") or []:
            if not isinstance(page, dict) or page.get("type") != "JOBS":
                continue
            for job in page.get("jobs") or []:
                if not isinstance(job, dict):
                    continue
                posting = self._normalize_one(job, ref)
                if posting is not None and posting.external_id not in seen:
                    seen.add(posting.external_id)
                    yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        if not title or not company:
            return None
        location_raw = (job.get("locations") or "").strip() or None

        excerpt_html = (job.get("description") or "").strip()
        excerpt = norm.html_to_text(excerpt_html) if excerpt_html else ""

        # "Yenimahalle, Ankara": district first, province last. The province is
        # the city every other source names.
        places = [p.strip() for p in (location_raw or "").split(",") if p.strip()]
        city = next((p for p in reversed(places) if _fold(p) not in _COUNTRY_WORDS), None)

        signal = f"{title} {location_raw or ''}"
        remote_type = "unknown"
        if _HYBRID.search(signal):
            remote_type = "hybrid"
        elif _REMOTE.search(signal):
            remote_type = "remote"

        low, high = _amount(job.get("salary_min")), _amount(job.get("salary_max"))
        stated = low is not None or high is not None

        url = job.get("url") or None
        return JobPosting(
            source=self.source_id,
            external_id=_identity(title, company, location_raw),
            market=ref.market,
            title=title,
            company_name=company,
            location_raw=location_raw,
            # tr_TR searches only Turkey, and Careerjet names the place without
            # the country, so the country is known even where the text omits it.
            country="TR",
            city=city,
            remote_type=remote_type,
            employment_type=_employment_type(ref),
            salary_min=low,
            salary_max=high,
            salary_currency=(job.get("salary_currency_code") or None) if stated else None,
            salary_period=_SALARY_PERIOD.get(str(job.get("salary_type") or "")) if stated else None,
            salary_is_stated=stated,
            description_html=excerpt_html or None,
            description_text=excerpt or None,
            description_md=norm.html_to_markdown(excerpt_html) if excerpt_html else None,
            jd_completeness="snippet" if excerpt else "none",
            jd_source="api" if excerpt else None,
            posted_at=norm.parse_datetime(job.get("date")),
            apply_url=url,
            source_url=url,
        )


def _identity(title: str, company: str, location: str | None) -> str:
    key = "|".join(_fold(part) for part in (title, company, location or ""))
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def _amount(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _employment_type(ref: BoardRef) -> str | None:
    """Known only when the search filtered on it; the job itself never says."""
    hours = {v: k for k, v in _WORK_HOURS.items()}.get(ref.extra.get("work_hours", ""))
    contract = {v: k for k, v in _CONTRACT.items()}.get(ref.extra.get("contract_type", ""))
    return hours or contract
