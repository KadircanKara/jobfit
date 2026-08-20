"""Rung 1: schema.org/JobPosting from a page's JSON-LD block.

Google requires this structured data for a listing to appear in Google Jobs, so
most boards, including nearly all the Turkish ones, embed a complete block on
every detail page for SEO. Free, deterministic, stable, and it should carry the
large majority of tier 5 on its own.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

from bs4 import BeautifulSoup

from jobhunt.pipeline import normalize as norm


@dataclasses.dataclass
class Extracted:
    description_html: str | None = None
    description_text: str | None = None
    title: str | None = None
    company: str | None = None
    company_domain: str | None = None
    country: str | None = None
    city: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    valid_through: str | None = None
    salary_text: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.description_text)


def extract(html: str | None) -> Extracted | None:
    """First JobPosting object found, or None. Never raises on bad markup."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _load(tag.string or tag.get_text() or "")
        posting = _find_job_posting(payload)
        if posting is not None:
            return _build(posting)
    return None


def _load(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some boards emit JSON-LD with trailing commas or embedded newlines in
        # strings. One repair attempt, then give up and let the next rung try.
        try:
            return json.loads(raw.replace("\n", " ").replace(",}", "}").replace(",]", "]"))
        except json.JSONDecodeError:
            return None


def _find_job_posting(node: Any, depth: int = 0) -> dict | None:
    """JobPosting can be top level, in @graph, or in a list. Walk for it."""
    if depth > 5:
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_job_posting(item, depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(str(t).lower() == "jobposting" for t in types if t):
        return node
    for key in ("@graph", "mainEntity", "itemListElement"):
        found = _find_job_posting(node.get(key), depth + 1)
        if found is not None:
            return found
    return None


def _build(posting: dict) -> Extracted:
    description_html = posting.get("description")
    if isinstance(description_html, dict):
        description_html = description_html.get("@value")
    description_html = norm.unescape_if_escaped(description_html) if description_html else None

    org = posting.get("hiringOrganization")
    company = None
    company_domain = None
    if isinstance(org, dict):
        company = org.get("name")
        company_domain = norm.domain_from_url(org.get("sameAs") or org.get("url"))
    elif isinstance(org, str):
        company = org

    country, city = _location(posting.get("jobLocation"))

    return Extracted(
        description_html=description_html,
        description_text=norm.html_to_text(description_html) if description_html else None,
        title=posting.get("title"),
        company=company,
        company_domain=company_domain,
        country=country,
        city=city,
        employment_type=_first_str(posting.get("employmentType")),
        posted_at=_first_str(posting.get("datePosted")),
        valid_through=_first_str(posting.get("validThrough")),
        salary_text=_salary(posting.get("baseSalary")),
    )


def _first_str(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if isinstance(value, str | int | float) else None


def _location(node: Any) -> tuple[str | None, str | None]:
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None, None
    address = node.get("address")
    if isinstance(address, list):
        address = address[0] if address else None
    if not isinstance(address, dict):
        return None, None
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    city = address.get("addressLocality")
    if isinstance(city, dict):
        city = city.get("name")
    code = norm.country_code(str(country)) if country else None
    return code, (str(city).strip() if city else None)


def _salary(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    currency = node.get("currency") or node.get("salaryCurrency") or ""
    if isinstance(value, dict):
        low = value.get("minValue")
        high = value.get("maxValue")
        unit = value.get("unitText") or ""
        if low or high:
            return f"{low or high}-{high or low} {currency} {unit}".strip()
        single = value.get("value")
        if single:
            return f"{single} {currency} {unit}".strip()
    elif value:
        return f"{value} {currency}".strip()
    return None
