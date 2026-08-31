"""LinkedIn's public guest job search.

Unlike every other adapter, this one was never probed against a live response
before being written: the guest search and guest job-detail pages refuse a
plain `httpx` fetch outside a browser context in this environment, so the
fixtures under `tests/fixtures/linkedin_*.html` are hand-written from the
class names and document shape described in the integration plan, not trimmed
from a captured payload. If LinkedIn's markup has drifted from that shape,
`normalize` degrades to zero postings for a page rather than raising — see the
try/except around each card below - so a class-name mismatch shows up as a
quiet drop in yield, not a crash. Task 8's wiring is the place a real run will
first prove or disprove the guess.

Two passes, same as the other sources: `fetch` walks the guest search result
pages for one title/location pair, then follows each new job's own detail page
for its full description and hiring-team block. "New" matters here in a way it
does not elsewhere - a detail fetch is a second request against a source that
is watching for exactly this kind of traffic, so ids already in the corpus
(passed in via `known_ids`, since `fetch` gets no database session) are never
re-fetched just to refresh a description that has not changed.

`CrawlGuard` governs both passes: pagination stops the moment it refuses, and
a 429 or 403 during either pass stops that pass too rather than working
through what is left on borrowed time. The detail loop paces itself with the
same `guard.delay()` sleep the search loop uses, including before its first
request, so a burst of detail fetches never lands back-to-back with each other
or with the search page that preceded them.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx
from bs4 import BeautifulSoup

from jobhunt import preferences as preferences_module
from jobhunt.config import Config
from jobhunt.pipeline import normalize as norm
from jobhunt.preferences import Preferences
from jobhunt.sources import linkedin_query as query
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit
from jobhunt.sources.linkedin_guard import CrawlGuard

# A real Accept-Language, because a guest fetch with none is more conspicuous
# than one with an ordinary browser's default. Deliberately not rotated, and
# deliberately not paired with a different User-Agent per request - see
# linkedin_guard's module docstring for why: those techniques evade a limit
# rather than respect it. The User-Agent itself comes from the shared client
# built in sync.fetch_pass, from config, same as every other source.
_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}


def _job_ids(cards_html: list[str]) -> list[str]:
    """External ids of every card across every fetched page, first-seen order."""
    seen: dict[str, None] = {}
    for html in cards_html:
        for job_id in _card_ids(html):
            seen.setdefault(job_id, None)
    return list(seen)


def _cards(html: str) -> list[Any]:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    return soup.select("div.base-card")


def _card_ids(html: str) -> list[str]:
    ids = []
    for card in _cards(html):
        urn = card.get("data-entity-urn") or ""
        job_id = urn.rsplit(":", 1)[-1]
        if job_id:
            ids.append(job_id)
    return ids


def _clean_url(href: str | None) -> str | None:
    """Drop the query string and a trailing slash, so ids compare cleanly."""
    if not href:
        return None
    return href.strip().split("?")[0].rstrip("/") or None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    # A NaN reaches `dt.timedelta(seconds=...)` inside the guard and raises
    # there instead of here; an inf or negative value is not a real wait.
    # Either way this header is malformed, not a real instruction to honour.
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return None
    return seconds


class LinkedInAdapter(HttpAdapter):
    source_id = "linkedin"
    market = "global_remote"
    rate_limit = RateLimit(3.0)
    MAX_PAGES = 5
    PAGE_SIZE = 10

    def __init__(
        self,
        refs: list[BoardRef] | None = None,
        *,
        config: Config | None = None,
        known_ids: set[str] | None = None,
    ) -> None:
        super().__init__(refs)
        self.config = config
        self.guard = CrawlGuard(config) if config is not None else None
        self.known_ids = known_ids or set()

    def board_refs(self, prefs: Preferences) -> list[BoardRef]:
        """One search per title and location. LinkedIn has no boards to seed."""
        locations = prefs.locations or [""]
        return [
            BoardRef(provider=self.source_id, token=f"{title}|{location}", market=self.market)
            for title in prefs.titles
            for location in locations
        ]

    # --- fetch ------------------------------------------------------------

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """Page the search, then fetch details for the ids the corpus doesn't have.

        Never raises on an HTTP problem: a 429 or 403 is recorded on the guard
        and ends the pass early, same as any other non-200. Only a config-less
        adapter (no guard at all) returns immediately - that shape exists for
        `normalize`-only tests and is never how a real run constructs this.
        """
        empty: dict[str, Any] = {"cards": [], "details": {}, "posters": {}}
        if self.guard is None:
            return empty

        try:
            prefs, _ = preferences_module.load(self.config)
        except Exception:
            # A malformed filters.yaml is a config problem, not a fetch problem.
            # This source is never allowed to fail a run over it.
            return empty
        title, _, location = ref.token.partition("|")

        cards: list[str] = []
        page = 0
        page_count = self.PAGE_SIZE  # primes the loop for a first request
        while self.guard.allow() and page_count == self.PAGE_SIZE and page < self.MAX_PAGES:
            params = query.search_params(title, location or None, prefs, start=page * self.PAGE_SIZE)
            self.guard.spend()
            try:
                response = client.get(query.SEARCH_URL, params=params, headers=_HEADERS)
            except httpx.HTTPError:
                break
            if response.status_code == 429:
                self.guard.record_429(_retry_after(response))
                break
            if response.status_code == 403:
                self.guard.record_403()
                break
            if response.status_code != 200:
                break
            self.guard.record_ok()
            html = response.text
            cards.append(html)
            page_count = len(_cards(html))
            page += 1
            if self.guard.allow() and page_count == self.PAGE_SIZE and page < self.MAX_PAGES:
                time.sleep(self.guard.delay())

        details: dict[str, str] = {}
        for job_id in _job_ids(cards):
            if not self.guard.allow():
                break
            if job_id in self.known_ids:
                continue
            # Paced like the search pages, including before this first detail
            # request: it follows the last search page and is otherwise the
            # one request in this adapter with no gap before it.
            time.sleep(self.guard.delay())
            self.guard.spend()
            try:
                response = client.get(query.DETAIL_URL.format(job_id=job_id), headers=_HEADERS)
            except httpx.HTTPError:
                continue
            if response.status_code == 429:
                self.guard.record_429(_retry_after(response))
                break
            if response.status_code == 403:
                self.guard.record_403()
                break
            if response.status_code != 200:
                continue
            self.guard.record_ok()
            details[job_id] = response.text

        # Reserved for a future pass: LinkedIn's hiring-team block is already
        # carried through via the detail HTML in `details`, so nothing needs
        # to be attached separately here yet. Kept in the envelope shape so
        # `normalize` and any later caller don't have to special-case its
        # absence.
        return {"cards": cards, "details": details, "posters": {}}

    # --- normalize ----------------------------------------------------------

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        """Raw envelope -> postings. Never raises: one bad card is one lost job.

        A shape a real fetch never actually produced ("cards" missing, a card
        missing the fields this parser expects) is exactly what the hand-written
        fixtures cannot rule out, so every card is isolated in its own
        try/except rather than trusted to be well-formed.
        """
        if not isinstance(raw, dict):
            return
        details = raw.get("details") or {}
        for html in raw.get("cards") or []:
            for card in _cards(html):
                try:
                    posting = self._normalize_card(card, ref, details)
                except Exception:
                    continue
                if posting is not None:
                    yield posting

    def _normalize_card(self, card: Any, ref: BoardRef, details: dict[str, str]) -> JobPosting | None:
        urn = card.get("data-entity-urn") or ""
        external_id = urn.rsplit(":", 1)[-1]
        title_el = card.select_one("h3.base-search-card__title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not external_id or not title:
            return None

        company_el = card.select_one("h4.base-search-card__subtitle a")
        company_name = company_el.get_text(strip=True) if company_el else "unknown"

        location_el = card.select_one("span.job-search-card__location")
        location_raw = location_el.get_text(strip=True) if location_el else None
        country, city, _parsed_remote = norm.parse_location(location_raw)

        time_el = card.select_one("time.job-search-card__listdate")
        posted_at = norm.parse_datetime(time_el.get("datetime")) if time_el else None

        link_el = card.select_one("a.base-card__full-link")
        source_url = _clean_url(link_el.get("href")) if link_el else None
        if source_url is None:
            source_url = f"https://www.linkedin.com/jobs/view/{external_id}"

        description_html, description_text, poster_name, poster_profile_url = (
            self._from_detail(details.get(external_id))
        )

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=company_name,
            location_raw=location_raw,
            country=country,
            city=city,
            # No `remote_type_from` helper exists in jobhunt.pipeline.normalize
            # yet, and guessing from `location_raw` alone here would be exactly
            # that - a guess dressed up as a signal. Left at the field default.
            description_html=description_html,
            description_text=description_text,
            description_md=norm.html_to_markdown(description_html) if description_html else None,
            jd_completeness="full" if description_html else "none",
            jd_source="html" if description_html else None,
            posted_at=posted_at,
            apply_url=source_url,
            source_url=source_url,
            poster_name=poster_name,
            poster_profile_url=poster_profile_url,
        )

    @staticmethod
    def _from_detail(
        detail_html: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """(description_html, description_text, poster_name, poster_profile_url)."""
        if not detail_html:
            return None, None, None, None
        soup = BeautifulSoup(detail_html, "html.parser")
        desc_el = soup.select_one("div.description__text")
        description_html = str(desc_el) if desc_el else None
        description_text = norm.html_to_text(description_html) if description_html else None

        poster_name: str | None = None
        poster_profile_url: str | None = None
        hirer = soup.select_one("div.hirer-card__hirer-information")
        if hirer is not None:
            link = hirer.select_one("a[href]")
            if link is not None:
                poster_profile_url = _clean_url(link.get("href"))
                poster_name = link.get_text(strip=True) or None

        return description_html, description_text, poster_name, poster_profile_url
