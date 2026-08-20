"""Arbeitnow job board API.

Verified 2026-08-20: 200, 175 records on page 1.

Two traps: the description is **HTML-escaped on the wire** exactly like
Greenhouse (`&lt;div&gt;`), and `location` is frequently an empty string while
the real location sits nowhere else, so an empty location must stay unknown
rather than being invented.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 5


class ArbeitnowAdapter(HttpAdapter):
    source_id = "arbeitnow"
    market = "global_remote"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        """Follow the API's own `links.next` rather than guessing page numbers."""
        merged: list[dict] = []
        url: str | None = BASE
        for page in range(MAX_PAGES):
            payload = self._get_json(client, url)
            merged.extend(payload.get("data") or [])
            links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
            url = links.get("next")
            if not url:
                break
            if page < MAX_PAGES - 1:
                self.rate_limit.sleep()
        return {"data": merged}

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for job in (raw or {}).get("data", []) if isinstance(raw, dict) else []:
            if not isinstance(job, dict):
                continue
            posting = self._normalize_one(job, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("slug") or "")
        title = (job.get("title") or "").strip()
        if not external_id or not title:
            return None

        # Escaped on the wire. Unescaping is not optional, same as Greenhouse.
        description_html = norm.unescape_if_escaped(job.get("description"))
        description_text = norm.html_to_text(description_html)

        location_raw = (job.get("location") or "").strip() or None
        country, city, parsed_remote = norm.parse_location(location_raw)
        remote_type = "remote" if job.get("remote") else parsed_remote

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=job.get("company_name") or "unknown",
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type=remote_type,
            employment_type=norm.normalize_employment_type(job.get("job_types")),
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("created_at")),
            apply_url=job.get("url"),
            source_url=job.get("url"),
            departments=[t for t in (job.get("tags") or [])[:3] if isinstance(t, str)],
        )
