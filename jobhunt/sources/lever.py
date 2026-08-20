"""Lever postings API.

Endpoint verified 2026-08-20 against token `matchgroup`: 200. The top level is a
bare JSON array, not an object, which is the first thing that breaks if you
assume every ATS looks like Greenhouse.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://api.lever.co/v0/postings/{token}?mode=json"

WORKPLACE_TYPE_MAP = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}

_PERIOD_MAP = {
    "per-year-salary": "annual", "per-year": "annual", "year": "annual", "annual": "annual",
    "per-month-salary": "monthly", "per-month": "monthly", "month": "monthly",
    "per-week-salary": "weekly", "per-week": "weekly", "week": "weekly",
    "per-hour-salary": "hourly", "per-hour": "hourly", "hour": "hourly",
}


class LeverAdapter(HttpAdapter):
    source_id = "lever"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        return self._get_json(client, BASE.format(token=ref.token))

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        # A bare array on success. Anything else is an error body, not postings.
        if not isinstance(raw, list):
            return
        for job in raw:
            if isinstance(job, dict):
                posting = self._normalize_one(job, ref)
                if posting is not None:
                    yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("id") or "")
        # The title field is `text`, not `title`.
        title = (job.get("text") or "").strip()
        if not external_id or not title:
            return None

        description_html = self._full_description(job)
        description_text = norm.html_to_text(description_html)
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}

        location_raw = (categories.get("location") or "").strip() or None
        country, city, parsed_remote = norm.parse_location(location_raw)
        # Lever carries an ISO country code of its own. It beats parsing the string.
        if job.get("country"):
            country = str(job["country"]).upper()[:2]

        workplace = (job.get("workplaceType") or "").strip().lower()
        remote_type = WORKPLACE_TYPE_MAP.get(workplace, parsed_remote)

        salary = self._salary(job.get("salaryRange"))

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=ref.extra.get("company_name") or ref.token,
            company_domain=None,  # Lever payloads carry no employer domain.
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type=remote_type,
            salary_min=salary[0],
            salary_max=salary[1],
            salary_currency=salary[2],
            salary_period=salary[3],
            salary_is_stated=salary[0] is not None or salary[1] is not None,
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("createdAt")),
            apply_url=job.get("applyUrl") or job.get("hostedUrl"),
            source_url=job.get("hostedUrl"),
            departments=[
                value
                for value in (categories.get("department"), categories.get("team"))
                if value
            ],
        )

    @staticmethod
    def _full_description(job: dict) -> str:
        """opening + body + lists + additional, in posting order.

        `description` is only the opening block on some boards, so taking it alone
        silently truncates the JD, and the tailoring skill must never receive a
        partial JD. Concatenating the parts is the only shape that holds across
        boards. `descriptionBody` excludes the opening, which is why both are used.
        """
        parts: list[str] = []
        for key in ("opening", "descriptionBody"):
            value = (job.get(key) or "").strip()
            if value:
                parts.append(value)
        if not parts:
            # Older boards populate only `description`, which bundles both.
            fallback = (job.get("description") or "").strip()
            if fallback:
                parts.append(fallback)
        for block in job.get("lists") or []:
            if not isinstance(block, dict):
                continue
            heading = (block.get("text") or "").strip()
            content = (block.get("content") or "").strip()
            if heading:
                parts.append(f"<h3>{heading}</h3>")
            if content:
                parts.append(content)
        additional = (job.get("additional") or "").strip()
        if additional:
            parts.append(additional)
        return "\n".join(parts)

    @staticmethod
    def _salary(salary_range: Any) -> tuple[float | None, float | None, str | None, str | None]:
        if not isinstance(salary_range, dict):
            return None, None, None, None
        def number(value: Any) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        interval = (salary_range.get("interval") or "").strip().lower()
        return (
            number(salary_range.get("min")),
            number(salary_range.get("max")),
            salary_range.get("currency") or None,
            _PERIOD_MAP.get(interval),
        )
