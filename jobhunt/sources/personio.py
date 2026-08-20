"""Personio XML job feed.

Endpoint verified 2026-08-20 against token `personio`: 200, text/xml.

XML, not JSON, so the raw payload stored on disk is the XML text rather than a
parsed structure. That keeps the fetch/normalize split intact: the stored bytes
are exactly what the server sent, and a parser change costs no request.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

BASE = "https://{token}.jobs.personio.de/xml?language=en"
JOB_URL = "https://{token}.jobs.personio.de/job/{job_id}"

# Personio states seniority itself, in its own vocabulary.
_SENIORITY_MAP = {
    "student": "intern", "entry_level": "junior", "experienced": "senior",
    "lead": "lead", "manager": "lead", "executive": "lead", "director": "lead",
}
_SCHEDULE_REMOTE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


class PersonioAdapter(HttpAdapter):
    source_id = "personio"
    market = "global_remote"
    rate_limit = RateLimit(1.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        response = client.get(BASE.format(token=ref.token))
        response.raise_for_status()
        return response.text

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, str) or not raw.strip():
            return
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            # A malformed feed is a source problem, not a pipeline problem. The
            # board simply produces nothing and gets aged by the scheduler.
            return
        for position in root:
            posting = self._normalize_one(position, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, position: ET.Element, ref: BoardRef) -> JobPosting | None:
        def text(tag: str) -> str:
            node = position.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""

        external_id = text("id")
        title = text("name")
        if not external_id or not title:
            return None

        description_html = self._description(position)
        description_text = norm.html_to_text(description_html)

        office = text("office")
        offices = [office] if office else []
        additional = position.find("additionalOffices")
        if additional is not None:
            offices += [(o.text or "").strip() for o in additional if o.text]
        location_raw = ", ".join(dict.fromkeys(o for o in offices if o)) or None
        country, city, parsed_remote = norm.parse_location(office or None)

        schedule = text("schedule").lower()
        remote_type = _SCHEDULE_REMOTE.get(schedule, parsed_remote)

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=text("subcompany") or ref.token,
            company_domain=None,
            location_raw=location_raw,
            country=country,
            city=city or (office or None),
            remote_type=remote_type,
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            posted_at=norm.parse_datetime(text("createdAt")),
            apply_url=JOB_URL.format(token=ref.token, job_id=external_id),
            source_url=JOB_URL.format(token=ref.token, job_id=external_id),
            departments=[d for d in (text("department"), text("recruitingCategory")) if d],
        )

    @staticmethod
    def _description(position: ET.Element) -> str:
        """jobDescriptions holds name/value pairs, each value a block of HTML."""
        container = position.find("jobDescriptions")
        if container is None:
            return ""
        parts: list[str] = []
        for block in container:
            name_node = block.find("name")
            value_node = block.find("value")
            heading = (name_node.text or "").strip() if name_node is not None and name_node.text else ""
            value = (value_node.text or "").strip() if value_node is not None and value_node.text else ""
            if heading:
                parts.append(f"<h3>{heading}</h3>")
            if value:
                parts.append(value)
        return "\n".join(parts)

    @staticmethod
    def stated_seniority(position: ET.Element) -> str | None:
        node = position.find("seniority")
        value = (node.text or "").strip().lower() if node is not None and node.text else ""
        return _SENIORITY_MAP.get(value)
