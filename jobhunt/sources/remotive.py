"""Remotive remote jobs API.

Verified 2026-08-20: 200, full description HTML per job.

`limit` is not honoured. The probe asked for 100 and got 17, then for 5 and got
17. Pagination has to be driven by what came back, never by what was requested,
so this adapter asks for the whole feed and takes what it gets.

Their legal notice asks for attribution. Storing and displaying source_url
satisfies that for a private tool. Nothing here redistributes.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://remotive.com/api/remote-jobs"

# "$36k", "$120,000 - $150,000 USD", "€60k-€80k". Parsed leniently: a wrong
# number is worse than no number, so anything ambiguous stays unstated.
_MONEY = re.compile(r"(\d[\d,.]*)\s*(k)?", re.I)
_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY"}


class RemotiveAdapter(HttpAdapter):
    source_id = "remotive"
    market = "global_remote"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        params = {"category": ref.token} if ref.token not in ("", "all") else None
        return self._get_json(client, BASE + (f"?category={ref.token}" if params else ""))

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for job in (raw or {}).get("jobs", []) if isinstance(raw, dict) else []:
            if not isinstance(job, dict):
                continue
            posting = self._normalize_one(job, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or "")
        title = (job.get("title") or "").strip()
        if not external_id or not title:
            return None

        description_html = job.get("description") or ""
        description_text = norm.html_to_text(description_html)
        location_raw = (job.get("candidate_required_location") or "").strip() or None
        country, city, _ = norm.parse_location(location_raw)
        low, high, currency = self._salary(job.get("salary"))

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=job.get("company_name") or "unknown",
            location_raw=location_raw,
            country=country,
            city=city,
            # Every posting on Remotive is a remote role. That is the whole site.
            remote_type="remote",
            employment_type=norm.normalize_employment_type(job.get("job_type")),
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_period="annual" if low or high else None,
            salary_is_stated=low is not None or high is not None,
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("publication_date")),
            apply_url=job.get("url"),
            source_url=job.get("url"),
            departments=[d for d in (job.get("category"),) if d],
        )

    @staticmethod
    def _salary(value: Any) -> tuple[float | None, float | None, str | None]:
        """Salary is a free-text string, so this is best effort and conservative."""
        if not isinstance(value, str) or not value.strip():
            return None, None, None
        currency = next((code for sign, code in _CURRENCY.items() if sign in value), None)
        numbers: list[float] = []
        for raw, suffix in _MONEY.findall(value):
            try:
                amount = float(raw.replace(",", ""))
            except ValueError:
                continue
            if suffix:
                amount *= 1000
            # Below 1000 with no k suffix is a year, a headcount, or noise.
            if amount >= 1000:
                numbers.append(amount)
        if not numbers:
            return None, None, currency
        return min(numbers), (max(numbers) if len(numbers) > 1 else None), currency
