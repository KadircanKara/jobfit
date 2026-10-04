"""kariyer.net, Turkey's largest job board. Searched by title, like LinkedIn.

There is no public API. The site's own search API signs every request with a
key from its runtime config (an `X-Hash` header), which is a door deliberately
kept shut, so this adapter does not use it. It reads the server-rendered pages
a browser gets instead, which robots.txt leaves open:

    GET /is-ilanlari?kw=<title>&<filters>&cp=<page>   about 50 cards a page
    GET /is-ilani/<slug>-<id>                          one posting

Filters are query parameters; see kariyer_net_query.py for the alphabet.

A card carries the title, company, city, work model, work type and age. The
description is only on the posting's own page, so, as on LinkedIn, the page is
fetched only for a card whose title stage 1 would keep and whose id the corpus
does not already have.

The age on a card is relative ("2 gün", "15 saat") and counts from the last
update, not the first publication; the posting page shows no date either. So
`posted_at` is the fetch time minus that age, which is the freshest the site
will say the job is.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from bs4 import BeautifulSoup

from jobhunt import preferences as preferences_module
from jobhunt.config import Config
from jobhunt.pipeline import normalize as norm
from jobhunt.sources import kariyer_net_query as query
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

log = logging.getLogger(__name__)

_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "tr,en;q=0.8"}

_JOB_ID = re.compile(r"-(\d+)/?$")

# Keyed on `query.fold`ed text; see there for why not `str.lower`.
_REMOTE_TYPE = {"is yerinde": "onsite", "uzaktan / remote": "remote", "hibrit": "hybrid"}

# Card badge text against our employment types. "Dönemsel" is the site's
# seasonal / project-based bucket and "Serbest zamanlı" its freelance one;
# both are what our "contract" asks the search for, so both read back as it.
_EMPLOYMENT = {
    "tam zamanli": "full_time",
    "yari zamanli": "part_time",
    "donemsel": "contract",
    "serbest zamanli": "contract",
}

_AGE = re.compile(r"(\d+)\s*(dakika|saat|gün|hafta|ay)", re.I)
_AGE_UNIT = {"dakika": 1 / 1440, "saat": 1 / 24, "gün": 1, "hafta": 7, "ay": 30}

# "İstanbul(Avr.)" is the European side, "İstanbul(Asya)" the Asian one, and a
# posting open in several cities reads "İstanbul(Asya) +2 il daha" (2 more).
_CITY_SUFFIX = re.compile(r"\s*(\(.*?\))?\s*(\+\s*\d+\s*il daha)?\s*$")


class KariyerNetAdapter(HttpAdapter):
    source_id = "kariyer_net"
    market = "tr_local"
    rate_limit = RateLimit(3.0)
    # Refs are title searches built from preferences every run, like LinkedIn's.
    generates_refs = True
    # About 50 cards a page. A title search rarely has more than a hundred
    # matches, and the ones past that are the least relevant.
    MAX_PAGES = 2
    PAGE_SIZE = 50

    def __init__(
        self,
        refs: list[BoardRef] | None = None,
        *,
        config: Config | None = None,
        known_ids: set[str] | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        super().__init__(refs)
        self.config = config
        self.known_ids = known_ids or set()
        self._now = now or (lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))
        self._truncated = False

    def board_refs(self, prefs: preferences_module.Preferences) -> list[BoardRef]:
        """One search per title. The site is Turkey-wide, so locations only narrow
        it to the Turkish cities named among them."""
        cities = query.city_ids(prefs.locations)
        return [
            BoardRef(
                provider=self.source_id,
                token=title,
                market=self.market,
                extra={"params": query.search_params(title, prefs, cities=cities)},
            )
            for title in prefs.titles
        ]

    def was_truncated(self) -> bool:
        return self._truncated

    # --- fetch ------------------------------------------------------------

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """The search pages for one title, then the postings worth reading.

        A 429 raises, so `sync.fetch_pass` waits it out and asks again. A 403 is
        the site's bot wall; the fetch stops there and keeps what it has.
        """
        self._truncated = False
        fetched_at = self._now().isoformat()
        params = dict(ref.extra.get("params") or {"kw": ref.token})
        pages: list[str] = []
        seen: set[str] = set()
        for page in range(1, self.MAX_PAGES + 1):
            if page > 1:
                self.rate_limit.sleep()
                params["cp"] = str(page)
            response = client.get(query.SEARCH_URL, params=params, headers=_HEADERS)
            if response.status_code == 403:
                log.warning("kariyer.net refused search %r (403); keeping %d pages", ref.token, len(pages))
                self._truncated = True
                break
            response.raise_for_status()
            cards = parse_cards(response.text)
            new = [c for c in cards if c["id"] not in seen]
            pages.append(response.text)
            seen.update(c["id"] for c in cards)
            # A short page is the last one; a page of nothing new means the site
            # is repeating its sponsored cards past the end.
            if len(cards) < self.PAGE_SIZE or not new:
                break

        details: dict[str, str] = {}
        wanted = _title_filter(self.config)
        pending = [
            card for page_html in pages for card in parse_cards(page_html)
            if card["id"] not in self.known_ids and wanted(card["title"])
        ]
        done: set[str] = set()
        for card in pending:
            if card["id"] in done:
                continue
            done.add(card["id"])
            self.rate_limit.sleep()
            try:
                response = client.get(query.BASE_URL + card["path"], headers=_HEADERS)
            except httpx.HTTPError:
                continue
            if response.status_code == 403:
                log.warning("kariyer.net refused a posting page (403); stopping details")
                self._truncated = True
                break
            if response.status_code == 429:
                response.raise_for_status()
            if response.status_code == 200:
                details[card["id"]] = response.text
        return {"fetched_at": fetched_at, "pages": pages, "details": details}

    # --- normalize ----------------------------------------------------------

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        """Raw envelope -> postings. One bad card is one lost job, never a raise."""
        if not isinstance(raw, dict):
            return
        fetched_at = norm.parse_datetime(raw.get("fetched_at"))
        details = raw.get("details") or {}
        seen: set[str] = set()
        for page_html in raw.get("pages") or []:
            for card in parse_cards(page_html):
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                try:
                    posting = _posting(card, details.get(card["id"]), ref, fetched_at)
                except Exception:  # noqa: BLE001 - a malformed card must not cost the page
                    log.exception("kariyer.net card %s did not parse", card.get("id"))
                    continue
                if posting is not None:
                    yield posting


def _title_filter(config: Config | None) -> Callable[[str | None], bool]:
    if config is None:
        return lambda title: True
    from jobhunt import board_scope

    return board_scope.title_filter(config)


def _text(node, selector: str) -> str:
    found = node.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def parse_cards(page_html: str) -> list[dict[str, str]]:
    """The job cards on one search page, in page order."""
    soup = BeautifulSoup(page_html, "html.parser")
    cards: list[dict[str, str]] = []
    for card in soup.select('[data-test="ad-card"]'):
        link = card.select_one('a[data-test="ad-card-item"]')
        href = (link.get("href") or "") if link else ""
        match = _JOB_ID.search(href.split("?")[0])
        if not match:
            continue
        cards.append({
            "id": match.group(1),
            "path": href.split("?")[0],
            "title": _text(card, '[data-test="ad-card-title"]'),
            "company": _text(card, '[data-test="subtitle"]'),
            "location": _text(card, '[data-test="location"]'),
            "work_model": _text(card, '[data-test="work-model"]'),
            "work_type": _text(card, '[data-test="mapped-badges"]'),
            "age": _text(card, '[data-test="ad-date"]'),
        })
    return cards


def _posted_at(age: str, fetched_at: dt.datetime | None) -> dt.datetime | None:
    if fetched_at is None:
        return None
    match = _AGE.search(age or "")
    if not match:
        return None
    days = int(match.group(1)) * _AGE_UNIT[match.group(2).lower()]
    return fetched_at - dt.timedelta(days=days)


def _posting(
    card: dict[str, str], detail_html: str | None, ref: BoardRef, fetched_at: dt.datetime | None
) -> JobPosting | None:
    title = card["title"].strip()
    company = card["company"].strip()
    description_html = ""
    if detail_html:
        soup = BeautifulSoup(detail_html, "html.parser")
        body = soup.select_one('[data-test="qualifications-and-job-description"]')
        description_html = body.decode_contents() if body else ""
        company = company or _text(soup, '[data-test="company-name"]')
    if not title or not company:
        return None
    description_text = norm.html_to_text(description_html) if description_html else ""

    city = _CITY_SUFFIX.sub("", card["location"]).strip() or None
    url = query.BASE_URL + card["path"]
    work_type = query.fold(card["work_type"])
    return JobPosting(
        source="kariyer_net",
        external_id=card["id"],
        market=ref.market,
        title=title,
        company_name=company,
        location_raw=card["location"] or None,
        country="TR",
        city=city,
        remote_type=_REMOTE_TYPE.get(query.fold(card["work_model"]), "unknown"),
        employment_type=_EMPLOYMENT.get(work_type) or norm.normalize_employment_type(work_type),
        description_html=description_html or None,
        description_text=description_text or None,
        description_md=norm.html_to_markdown(description_html) if description_html else None,
        jd_completeness=norm.completeness(description_text) if description_text else "none",
        jd_source="html" if description_text else None,
        posted_at=_posted_at(card["age"], fetched_at),
        apply_url=url,
        source_url=url,
    )
