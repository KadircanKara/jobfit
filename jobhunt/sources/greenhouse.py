"""Greenhouse board API.

Endpoint verified 2026-08-20 against token `stripe`: 200, 576 jobs.
Response shape and quirks recorded in references/sources.md.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

# Boards that put the work mode in location.name and the real city in a metadata
# entry. Verified: metadata is null on many boards, so every read is defensive.
_LOCATION_METADATA_KEYS = {"job posting location", "location", "office location", "city"}


class GreenhouseAdapter(HttpAdapter):
    source_id = "greenhouse"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        return self._get_json(client, BASE.format(token=ref.token))

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for job in (raw or {}).get("jobs", []):
            posting = self._normalize_one(job, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not external_id or not title:
            return None

        # content is HTML-escaped on the wire. Unescaping is not optional.
        description_html = norm.unescape_if_escaped(job.get("content"))
        description_text = norm.html_to_text(description_html)
        description_md = norm.html_to_markdown(description_html)

        location_raw = self._location(job)
        country, city, remote_type = norm.parse_location(location_raw)

        absolute_url = job.get("absolute_url")
        company_name = (job.get("company_name") or ref.token).strip()

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=company_name,
            # Stripe's absolute_url is stripe.com, not greenhouse.io, so this
            # legitimately resolves a real employer domain some of the time.
            company_domain=norm.domain_from_url(absolute_url),
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type=remote_type,
            description_html=description_html,
            description_text=description_text,
            description_md=description_md,
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("first_published") or job.get("updated_at")),
            apply_url=absolute_url,
            source_url=absolute_url,
            departments=[d.get("name") for d in (job.get("departments") or []) if d.get("name")],
        )

    @staticmethod
    def _location(job: dict) -> str | None:
        """Prefer a metadata location over location.name.

        Some boards put only the work mode ("Remote") in location.name and the real
        city in a metadata entry. metadata is null on most boards, so this walks it
        defensively rather than indexing into it.
        """
        metadata = job.get("metadata")
        if isinstance(metadata, list):
            for entry in metadata:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("name") or "").strip().lower()
                value = entry.get("value")
                if name in _LOCATION_METADATA_KEYS and value:
                    if isinstance(value, list):
                        value = ", ".join(str(v) for v in value if v)
                    text = str(value).strip()
                    if text:
                        return text
        location = job.get("location")
        if isinstance(location, dict):
            return (location.get("name") or "").strip() or None
        return None
