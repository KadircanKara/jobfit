"""Jobicy remote jobs API.

Verified 2026-08-20: 200, structured salary fields and full description HTML,
which makes it the best-shaped of the tier-2 aggregators.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://jobicy.com/api/v2/remote-jobs?count={count}"
COUNT = 50

_PERIOD_MAP = {
    "yearly": "annual", "annual": "annual", "year": "annual",
    "monthly": "monthly", "month": "monthly",
    "weekly": "weekly", "hourly": "hourly",
}


class JobicyAdapter(HttpAdapter):
    source_id = "jobicy"
    market = "global_remote"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        url = BASE.format(count=COUNT)
        if ref.token not in ("", "all"):
            url += f"&industry={ref.token}"
        return self._get_json(client, url)

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for job in (raw or {}).get("jobs", []) if isinstance(raw, dict) else []:
            if not isinstance(job, dict):
                continue
            posting = self._normalize_one(job, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or "")
        title = (job.get("jobTitle") or "").strip()
        if not external_id or not title:
            return None

        description_html = job.get("jobDescription") or ""
        description_text = norm.html_to_text(description_html)
        location_raw = (job.get("jobGeo") or "").strip() or None
        country, city, _ = norm.parse_location(location_raw)

        low = self._number(job.get("salaryMin"))
        high = self._number(job.get("salaryMax"))

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=job.get("companyName") or "unknown",
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type="remote",
            employment_type=norm.normalize_employment_type(job.get("jobType")),
            salary_min=low,
            salary_max=high,
            salary_currency=job.get("salaryCurrency") or None,
            salary_period=_PERIOD_MAP.get((job.get("salaryPeriod") or "").lower()),
            salary_is_stated=low is not None or high is not None,
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("pubDate")),
            apply_url=job.get("url"),
            source_url=job.get("url"),
            departments=[t for t in (job.get("jobIndustry") or []) if isinstance(t, str)][:3],
        )

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value) if value not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return None
        return number
