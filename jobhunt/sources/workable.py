"""Workable account widget API.

Unresolved in phase 2 and phase 3, resolved in phase 4 with real tokens from the
Common Crawl backfill. The endpoint was never the problem: every guessed token
answered 200 with an empty jobs array, which reads exactly like a working
endpoint with nothing to say. Given a token that actually exists, the same call
returns full postings with descriptions.

    GET /api/v1/widget/accounts/{token}?details=true  -> 20 jobs with description
    POST /api/v3/accounts/{token}/jobs                -> 18 jobs, no description

v1 is used because it carries the description. v3 would cost a detail fetch per
posting for the same data.

The token in the URL is the *account*, but the per-job URL is
`apply.workable.com/j/{shortcode}` with no account in it, so a job's own URL
cannot be used to rediscover its board. Account tokens come from
`apply.workable.com/{token}/j/{id}` URLs, which is what the crawl index holds.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"


class WorkableAdapter(HttpAdapter):
    source_id = "workable"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        return self._get_json(client, BASE.format(token=ref.token))

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, dict):
            return
        account_name = raw.get("name")
        for job in raw.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            posting = self._normalize_one(job, ref, account_name)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef, account_name: Any) -> JobPosting | None:
        external_id = str(job.get("shortcode") or "")
        title = (job.get("title") or "").strip()
        if not external_id or not title:
            return None

        description_html = job.get("description") or ""
        description_text = norm.html_to_text(description_html)

        city = (job.get("city") or "").strip() or None
        country_name = (job.get("country") or "").strip() or None
        parts = [p for p in (city, job.get("state"), country_name) if p]
        country = norm.country_code(country_name) if country_name else None
        # locations[] carries a real ISO code; the top-level country is a name.
        for location in job.get("locations") or []:
            if isinstance(location, dict) and location.get("countryCode"):
                country = str(location["countryCode"]).upper()[:2]
                break

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=(account_name or ref.token),
            location_raw=", ".join(parts) or None,
            country=country,
            city=city,
            remote_type="remote" if job.get("telecommuting") else ("onsite" if city else "unknown"),
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("published_on") or job.get("created_at")),
            apply_url=job.get("application_url") or job.get("url"),
            source_url=job.get("url") or job.get("shortlink"),
            departments=[d for d in (job.get("department"), job.get("function")) if d],
        )
