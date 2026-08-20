"""Ashby posting API.

Endpoint verified 2026-08-20 against token `ramp`: 200, 136 jobs.
Response shape and quirks recorded in references/sources.md.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"

WORKPLACE_TYPE_MAP = {"onsite": "onsite", "on_site": "onsite", "remote": "remote", "hybrid": "hybrid"}

# Ashby sends intervals like "1 YEAR" and "NONE", not bare unit names.
_PERIOD_MAP = {
    "year": "annual", "years": "annual", "yearly": "annual", "annual": "annual",
    "month": "monthly", "months": "monthly", "monthly": "monthly",
    "week": "weekly", "weeks": "weekly", "weekly": "weekly",
    "hour": "hourly", "hours": "hourly", "hourly": "hourly",
    "day": "daily", "days": "daily", "daily": "daily",
}
_INTERVAL_UNIT = re.compile(r"[a-z]+")


def _period(interval: str | None) -> str | None:
    """"1 YEAR" -> "annual". "NONE" and anything unrecognised -> None."""
    if not interval:
        return None
    match = _INTERVAL_UNIT.search(interval.strip().lower())
    return _PERIOD_MAP.get(match.group(0)) if match else None


class AshbyAdapter(HttpAdapter):
    source_id = "ashby"
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
        # isListed false means the posting is not public. Do not surface it.
        if job.get("isListed") is False:
            return None

        description_html = job.get("descriptionHtml") or ""
        description_text = job.get("descriptionPlain") or norm.html_to_text(description_html)
        description_md = norm.html_to_markdown(description_html)

        location_raw = (job.get("location") or "").strip() or None
        country, city, parsed_remote = norm.parse_location(location_raw)
        country, city = self._from_postal_address(job, country, city)

        # workplaceType is authoritative. isRemote disagrees with it on real data
        # (ramp returns workplaceType Hybrid with isRemote true), so it is ignored.
        workplace = (job.get("workplaceType") or "").strip().lower()
        remote_type = WORKPLACE_TYPE_MAP.get(workplace, parsed_remote)

        salary = self._compensation(job.get("compensation"))

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=ref.extra.get("company_name") or ref.token,
            company_domain=None,  # Ashby payloads carry no employer domain.
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
            description_md=description_md,
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(job.get("publishedAt")),
            apply_url=job.get("applyUrl") or job.get("jobUrl"),
            source_url=job.get("jobUrl"),
            departments=[d for d in (job.get("department"), job.get("team")) if d],
        )

    @staticmethod
    def _from_postal_address(
        job: dict, country: str | None, city: str | None
    ) -> tuple[str | None, str | None]:
        """address.postalAddress is schema.org shaped and beats parsing the string."""
        address = job.get("address")
        if not isinstance(address, dict):
            return country, city
        postal = address.get("postalAddress")
        if not isinstance(postal, dict):
            return country, city
        raw_country = (postal.get("addressCountry") or "").strip()
        if raw_country:
            country = norm.country_code(raw_country) or country
        locality = (postal.get("addressLocality") or "").strip()
        if locality:
            city = locality
        return country, city

    @staticmethod
    def _compensation(comp: Any) -> tuple[float | None, float | None, str | None, str | None]:
        """Read the structured components, never the summary strings.

        compensationTiers[].components[] carries compensationType Salary alongside
        EquityPercentage; only the salary components have usable numbers.
        """
        if not isinstance(comp, dict):
            return None, None, None, None
        lo: float | None = None
        hi: float | None = None
        currency: str | None = None
        period: str | None = None
        for tier in comp.get("compensationTiers") or []:
            if not isinstance(tier, dict):
                continue
            for component in tier.get("components") or []:
                if not isinstance(component, dict):
                    continue
                if component.get("compensationType") != "Salary":
                    continue
                min_value = component.get("minValue")
                max_value = component.get("maxValue")
                if min_value is None and max_value is None:
                    continue
                if min_value is not None:
                    lo = float(min_value) if lo is None else min(lo, float(min_value))
                if max_value is not None:
                    hi = float(max_value) if hi is None else max(hi, float(max_value))
                currency = currency or component.get("currencyCode")
                period = period or _period(component.get("interval"))
        return lo, hi, currency, period
