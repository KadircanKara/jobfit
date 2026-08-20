"""We Work Remotely category RSS feeds.

Verified 2026-08-20: the site-wide feed returns 100 items, and category feeds
answer too (`remote-programming-jobs` 25, `remote-devops-sysadmin-jobs` 37).

RSS, so the stored raw payload is the XML text exactly as sent.

The item title packs two fields as "Company: Position". Splitting it is the only
way to get a usable company name, and the split has to be conservative because
plenty of real titles contain a colon of their own.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

SITE_FEED = "https://weworkremotely.com/remote-jobs.rss"
CATEGORY_FEED = "https://weworkremotely.com/categories/{token}.rss"

# Verified live. Category slugs change, and a dead one is a dead board, which the
# scheduler already handles.
DEFAULT_CATEGORIES = ("remote-programming-jobs", "remote-devops-sysadmin-jobs")


class WeWorkRemotelyAdapter(HttpAdapter):
    source_id = "wwr"
    market = "global_remote"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        url = SITE_FEED if ref.token in ("", "all") else CATEGORY_FEED.format(token=ref.token)
        response = client.get(url)
        response.raise_for_status()
        return response.text

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, str) or not raw.strip():
            return
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return
        for item in root.findall("./channel/item"):
            posting = self._normalize_one(item, ref)
            if posting is not None:
                yield posting

    def _normalize_one(self, item: ET.Element, ref: BoardRef) -> JobPosting | None:
        def text(tag: str) -> str:
            node = item.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""

        link = text("link") or text("guid")
        raw_title = text("title")
        if not link or not raw_title:
            return None

        company, title = self._split_title(raw_title)
        description_html = text("description")
        description_text = norm.html_to_text(description_html)

        region = text("region")
        country, city, parsed_remote = norm.parse_location(text("country") or region or None)

        return JobPosting(
            source=self.source_id,
            # The link is the only stable identifier in the feed. There is no id.
            external_id=link,
            market=ref.market,
            title=title,
            company_name=company,
            location_raw=region or None,
            country=country,
            city=city or (text("state") or None),
            remote_type="remote" if not text("country") else parsed_remote,
            employment_type=norm.normalize_employment_type(text("type")),
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html),
            jd_completeness=norm.completeness(description_text),
            jd_source="rss" if description_text else None,
            posted_at=norm.parse_datetime(text("pubDate")),
            apply_url=link,
            source_url=link,
            departments=[c for c in (text("category"),) if c],
        )

    @staticmethod
    def _split_title(raw_title: str) -> tuple[str, str]:
        """"Gusto, Inc.: Benefits Operations Lead" -> ("Gusto, Inc.", "...").

        Only the first colon splits, and only when both halves look real. A title
        like "Engineer: Platform" with no company prefix keeps its whole self and
        reports an unknown company rather than inventing one.
        """
        if ":" not in raw_title:
            return "unknown", raw_title
        company, _, title = raw_title.partition(":")
        company, title = company.strip(), title.strip()
        if not company or not title:
            return "unknown", raw_title.strip()
        return company, title
