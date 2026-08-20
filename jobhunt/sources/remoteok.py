"""RemoteOK API.

Verified 2026-08-20: 200, 101 records. **The first array element is a legal
notice, not a job**, exactly as PLAN.md warns. Treating it as a job would create
a phantom posting with no title on every single run.

Their terms ask for a followed backlink. Storing source_url satisfies that for a
private tool; nothing here republishes.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://remoteok.com/api"


class RemoteOkAdapter(HttpAdapter):
    source_id = "remoteok"
    market = "global_remote"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        url = BASE if ref.token in ("", "all") else f"{BASE}?tag={ref.token}"
        return self._get_json(client, url)

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, list):
            return
        for entry in raw:
            # The notice element has `legal` and no `position`. Filter on the
            # shape rather than on the index, in case the order ever changes.
            if not isinstance(entry, dict) or entry.get("legal") or not entry.get("position"):
                continue
            posting = self._normalize_one(entry, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or job.get("slug") or "")
        title = (job.get("position") or "").strip()
        if not external_id or not title:
            return None

        description_html = job.get("description") or ""
        description_text = norm.html_to_text(description_html)
        location_raw = (job.get("location") or "").strip(" ,") or None
        country, city, _ = norm.parse_location(location_raw)

        # 0 is RemoteOK's "not stated", not a free job.
        low = float(job["salary_min"]) if job.get("salary_min") else None
        high = float(job["salary_max"]) if job.get("salary_max") else None

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=job.get("company") or "unknown",
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type="remote",
            salary_min=low,
            salary_max=high,
            salary_currency="USD" if low or high else None,
            salary_period="annual" if low or high else None,
            salary_is_stated=low is not None or high is not None,
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("epoch") or job.get("date")),
            apply_url=job.get("apply_url") or job.get("url"),
            source_url=job.get("url"),
            departments=[t for t in (job.get("tags") or [])[:3] if isinstance(t, str)],
        )
