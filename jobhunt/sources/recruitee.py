"""Recruitee offers API.

Endpoint verified 2026-08-20 against token `channable`: 200, 15 offers.

Note for discovery: `careers_url` points at the employer's own domain
(jobs.channable.com), not at recruitee.com, so Strategy A does not harvest
Recruitee tokens from Recruitee postings. Certificate transparency does, because
the token is the subdomain. That is Strategy D, phase 4.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://{token}.recruitee.com/api/offers/"

_PERIOD_MAP = {
    "year": "annual", "yearly": "annual", "annual": "annual",
    "month": "monthly", "monthly": "monthly",
    "week": "weekly", "weekly": "weekly",
    "hour": "hourly", "hourly": "hourly",
    "day": "daily", "daily": "daily",
}


class RecruiteeAdapter(HttpAdapter):
    source_id = "recruitee"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        return self._get_json(client, BASE.format(token=ref.token))

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        for offer in (raw or {}).get("offers", []) if isinstance(raw, dict) else []:
            if not isinstance(offer, dict):
                continue
            posting = self._normalize_one(offer, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, offer: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(offer.get("id") or "")
        title = (offer.get("title") or offer.get("position") or "").strip()
        if not external_id or not title:
            return None
        # Drafts and closed offers are in the same feed as published ones.
        if (offer.get("status") or "published") != "published":
            return None

        # Description is split across two fields. Either one alone is a partial JD.
        description_html = "\n".join(
            part for part in (offer.get("description"), offer.get("requirements")) if part
        )
        description_text = norm.html_to_text(description_html)

        # Three booleans, not one enum. on_site is the default when all are false.
        if offer.get("remote"):
            remote_type = "remote"
        elif offer.get("hybrid"):
            remote_type = "hybrid"
        elif offer.get("on_site"):
            remote_type = "onsite"
        else:
            remote_type = "unknown"

        salary = self._salary(offer.get("salary"))
        careers_url = offer.get("careers_url")

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=offer.get("company_name") or ref.token,
            company_domain=norm.domain_from_url(careers_url),
            location_raw=offer.get("location") or offer.get("city"),
            country=(offer.get("country_code") or "").upper()[:2] or None,
            city=offer.get("city") or None,
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
            posted_at=norm.parse_datetime(offer.get("published_at") or offer.get("created_at")),
            apply_url=offer.get("careers_apply_url") or careers_url,
            source_url=careers_url,
            departments=[d for d in (offer.get("department"),) if d],
        )

    @staticmethod
    def _salary(salary: Any) -> tuple[float | None, float | None, str | None, str | None]:
        """min and max arrive as strings, not numbers."""
        if not isinstance(salary, dict):
            return None, None, None, None
        def number(value: Any) -> float | None:
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None
        period = (salary.get("period") or "").strip().lower()
        return (
            number(salary.get("min")),
            number(salary.get("max")),
            salary.get("currency") or None,
            _PERIOD_MAP.get(period),
        )
