"""SmartRecruiters postings API.

Endpoint verified 2026-08-20 against companyId `Visa`: 200, 2 postings.

The list response carries **no description**, which is confirmed behaviour, not a
missing parameter. Every posting therefore lands with jd_completeness "none" and
its `ref` detail URL stored in source_url, for the phase-5 extraction ladder to
pull lazily, one job at a time, after ranking. Bulk-fetching those details here
would be exactly the crawler behaviour PLAN.md non-negotiable 10 forbids.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}"
PAGE_SIZE = 100
MAX_PAGES = 10


class SmartRecruitersAdapter(HttpAdapter):
    source_id = "smartrecruiters"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        """Page until the API stops returning content, capped.

        totalFound can exceed what one call returns, and a board with 900 open
        roles is real. The cap keeps a single board from monopolising a run.
        """
        content: list[dict] = []
        total = 0
        for page in range(MAX_PAGES):
            payload = self._get_json(client, BASE.format(token=ref.token, offset=page * PAGE_SIZE))
            batch = payload.get("content") or []
            total = payload.get("totalFound") or total
            content.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            self.rate_limit.sleep()
        return {"totalFound": total, "content": content}

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for posting in (raw or {}).get("content", []) if isinstance(raw, dict) else []:
            if not isinstance(posting, dict):
                continue
            result = self._normalize_one(posting, ref)
            if result is not None:
                yield result

    def _normalize_one(self, posting: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(posting.get("id") or "")
        # The title field is `name`.
        title = (posting.get("name") or "").strip()
        if not external_id or not title:
            return None
        if (posting.get("visibility") or "PUBLIC").upper() != "PUBLIC":
            return None

        location = posting.get("location") if isinstance(posting.get("location"), dict) else {}
        company = posting.get("company") if isinstance(posting.get("company"), dict) else {}
        city = (location.get("city") or "").strip() or None
        country = (location.get("country") or "").strip().upper()[:2] or None
        parts = [p for p in (city, location.get("region"), location.get("country")) if p]

        if location.get("remote"):
            remote_type = "remote"
        elif location.get("hybrid"):
            remote_type = "hybrid"
        else:
            remote_type = "onsite" if city else "unknown"

        token = company.get("identifier") or ref.token
        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=company.get("name") or ref.token,
            company_domain=None,
            location_raw=", ".join(parts) or None,
            country=country,
            city=city,
            remote_type=remote_type,
            description_html=None,
            description_text=None,
            description_md=None,
            # No description on the wire. Saying "snippet" here would let a
            # partial JD reach the tailoring skill, which is non-negotiable 9.
            jd_completeness="none",
            jd_source=None,
            posted_at=norm.parse_datetime(posting.get("releasedDate")),
            apply_url=f"https://jobs.smartrecruiters.com/{token}/{external_id}",
            source_url=posting.get("ref"),
            departments=[
                value.get("label")
                for value in (posting.get("department"), posting.get("function"))
                if isinstance(value, dict) and value.get("label")
            ],
        )
