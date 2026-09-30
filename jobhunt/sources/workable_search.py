"""Workable's public cross-company job search, the one behind jobs.workable.com.

The `workable` adapter can only fetch a company it already knows the account
token of, and those tokens come from the Common Crawl backfill. Every Workable
company that crawl never saw is invisible to it. This source asks Workable's
own job search instead - by title and location, the way the LinkedIn source
does - so no board is needed at all.

Probed live (2026-09-30):

    GET https://jobs.workable.com/api/v1/jobs?query=<title>&location=<place>
        -> {"totalSize": 16, "jobs": [...20 at most], "nextPageToken": "..."}
    same URL + &pageToken=<nextPageToken>  -> the next 20, no overlap
    &workplace=remote&workplace=hybrid     -> repeated, not comma-joined (that
                                              returns nothing at all)

Each job carries its full description (plus separate requirements and benefits
sections), the company's display name, the workplace type and a structured
location, so there is no detail fetch.

What it does not carry is anything the per-board adapter keys on: no shortcode
and no account token, on the search result, the job detail
(`/api/v1/jobs/{id}`), the company endpoint, or the job's own page. So a job
found here cannot be given the board adapter's `(workable, shortcode)`
identity, and it is stored under its own source with Workable's search id.
The same posting fetched from its company board is folded into one card by the
cross-source clustering in pipeline/dedupe.py - same company name, same
normalized title, the same description body - exactly the way an aggregator's
copy of a board job already is.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt import preferences as preferences_module
from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

SEARCH_URL = "https://jobs.workable.com/api/v1/jobs"

# Workable's workplace values against ours, both directions.
_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "on_site"}
_REMOTE_TYPE = {"remote": "remote", "hybrid": "hybrid", "on_site": "onsite"}

# Location words that mean "anywhere" rather than naming a place. Sent as a
# location, Workable would search for a place called "Remote".
_ANYWHERE = frozenset({"", "anywhere", "worldwide", "remote", "global"})

# Workable's marker for a remote posting inside `locations`, not a place.
_TELECOMMUTE = "TELECOMMUTE"


class WorkableSearchAdapter(HttpAdapter):
    source_id = "workable_search"
    market = "global_remote"
    rate_limit = RateLimit(2.0)
    # Refs are title|location searches built from preferences every run, like
    # LinkedIn's. See SourceAdapter.generates_refs.
    generates_refs = True
    # 20 a page. A broad title with no location matches thousands of jobs
    # ordered by relevance; the first hundred are the ones worth having.
    MAX_PAGES = 5

    def __init__(self, refs: list[BoardRef] | None = None) -> None:
        super().__init__(refs)
        # Pages already collected for a search a 429 interrupted, by ref token.
        # `sync.fetch_pass` waits the 429 out and calls `fetch` again with the
        # same ref; this lets it pick up at the page it was refused on rather
        # than asking for the earlier pages a second time.
        self._resume: dict[str, tuple[list[Any], str | None]] = {}

    def board_refs(self, prefs: preferences_module.Preferences) -> list[BoardRef]:
        """One search per title and location. There are no boards to seed."""
        workplace = sorted({_WORKPLACE[m] for m in prefs.work_model if m in _WORKPLACE})
        locations = prefs.locations or [""]
        return [
            BoardRef(
                provider=self.source_id,
                token=f"{title}|{location}",
                market=self.market,
                extra={"workplace": workplace},
            )
            for title in prefs.titles
            for location in locations
        ]

    def params(self, ref: BoardRef, page_token: str | None) -> list[tuple[str, str]]:
        """Query params for one page. A list, because `workplace` repeats."""
        title, _, location = ref.token.partition("|")
        params = [("query", title)]
        if location.strip().lower() not in _ANYWHERE:
            params.append(("location", location.strip()))
        # All three ticked filters nothing, so it is not sent at all.
        workplace = ref.extra.get("workplace") or []
        if len(workplace) < len(_WORKPLACE):
            params += [("workplace", w) for w in workplace]
        if page_token:
            params.append(("pageToken", page_token))
        return params

    # --- fetch ------------------------------------------------------------

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """Every page of one search, up to MAX_PAGES.

        A 429 raises, the way every board fetch does, so `sync.fetch_pass`
        waits it out through sources/throttle.py and asks again - and this
        resumes from the refused page (see `_resume`).
        """
        pages, page_token = self._resume.pop(ref.token, ([], None))
        while len(pages) < self.MAX_PAGES:
            if pages:
                self.rate_limit.sleep()
            response = client.get(SEARCH_URL, params=self.params(ref, page_token))
            if response.status_code == 429:
                self._resume[ref.token] = (pages, page_token)
            response.raise_for_status()
            page = response.json()
            pages.append(page)
            page_token = page.get("nextPageToken") if isinstance(page, dict) else None
            if not page_token or not (page.get("jobs") if isinstance(page, dict) else None):
                break
        return {"pages": pages}

    # --- normalize ----------------------------------------------------------

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, dict):
            return
        seen: set[str] = set()
        for page in raw.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for job in page.get("jobs") or []:
                if not isinstance(job, dict):
                    continue
                posting = self._normalize_one(job, ref)
                # Relevance order can shift between two pages of one search.
                if posting is not None and posting.external_id not in seen:
                    seen.add(posting.external_id)
                    yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        company = job.get("company") if isinstance(job.get("company"), dict) else {}
        company_name = (company.get("title") or "").strip()
        if not external_id or not title or not company_name:
            return None
        if job.get("state") not in (None, "published"):
            return None

        description_html = _description(job)
        description_text = norm.html_to_text(description_html) if description_html else None

        location = job.get("location") if isinstance(job.get("location"), dict) else {}
        city = (location.get("city") or "").strip() or None
        country = norm.country_code(location.get("countryName"))
        places = [p for p in job.get("locations") or [] if isinstance(p, str) and p != _TELECOMMUTE]
        remote_type = _REMOTE_TYPE.get(str(job.get("workplace") or ""), "unknown")

        url = job.get("url") or None
        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=company_name,
            location_raw="; ".join(places) or location.get("countryName") or None,
            country=country,
            city=city,
            remote_type=remote_type,
            employment_type=norm.normalize_employment_type(job.get("employmentType")),
            description_html=description_html or None,
            description_text=description_text or None,
            description_md=norm.html_to_markdown(description_html) if description_html else None,
            description_lang=job.get("language") or None,
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("created")),
            # A posting that applies off Workable says where; otherwise the
            # apply button is on the job's own jobs.workable.com page.
            apply_url=job.get("linkoutUrl") or url,
            source_url=url,
            departments=[job["department"]] if job.get("department") else [],
        )


def _description(job: dict) -> str:
    """The body, then requirements and benefits, which search returns apart.

    A company board's copy of the same posting carries all three in one body;
    joined here, the two copies hash alike and cluster as one job.
    """
    parts = [job.get("description") or ""]
    for heading, key in (("Requirements", "requirementsSection"), ("Benefits", "benefitsSection")):
        section = job.get(key) or ""
        if section.strip():
            parts.append(f"<h3>{heading}</h3>{section}")
    return "".join(parts).strip()
