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

import logging
import math
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from jobhunt import preferences as preferences_module
from jobhunt.config import Config
from jobhunt.pipeline import normalize as norm
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

log = logging.getLogger(__name__)

_BASE = "https://www.linkedin.com"


def _job_ids(cards_html: list[str]) -> list[str]:
    """External ids of every card across every fetched page, first-seen order."""
    seen: dict[str, None] = {}
    for html in cards_html:
        for job_id in _card_ids(html):
            seen.setdefault(job_id, None)
    return list(seen)


def _card_titles(cards_html: list[str]) -> dict[str, str]:
    """Job id -> the title its search card shows, across every fetched page."""
    titles: dict[str, str] = {}
    for html in cards_html:
        for card in _cards(html):
            job_id = (card.get("data-entity-urn") or "").rsplit(":", 1)[-1]
            title_el = card.select_one("h3.base-search-card__title")
            if job_id and job_id not in titles:
                titles[job_id] = title_el.get_text(strip=True) if title_el else ""
    return titles


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
    """Absolute URL, minus the query string, fragment, and a trailing slash.

    The hirer card commonly emits a relative `/in/slug`. Stored verbatim it
    contains no `linkedin.com/in/`, so the Unipile provider cannot recognise a
    profile and refuses the send, and the drawer renders `https:///in/slug`.

    Deliberately leaves the host alone: this also cleans `apply_url`, whose
    regional subdomain (`de.linkedin.com/jobs/view/...`) is part of the link
    the applicant is meant to follow, not an artefact to normalise away. See
    `_clean_profile_url` for the poster-URL case, where the host *is* noise.
    """
    if not href:
        return None
    text = href.strip().split("?", 1)[0].split("#", 1)[0]
    if not text:
        return None
    return urljoin(_BASE, text).rstrip("/") or None


def _clean_profile_url(href: str | None) -> str | None:
    """`_clean_url`, plus canonicalising the host to `www.linkedin.com`.

    The guest job-poster block links to the poster's *regional* subdomain
    (`uk.linkedin.com/in/...`). `normalize_profile_url` only strips a `www.`
    prefix, not an arbitrary regional one, so a stored `uk.linkedin.com` URL
    would dedupe as a different person from the same profile reached via
    `www.linkedin.com` - and would still resolve through Unipile today only
    because `_PROFILE_MARKER` is a substring check, not a host check.
    Canonicalising here, the same way `source_url` is already built, keeps
    that from being an accident.
    """
    cleaned = _clean_url(href)
    if cleaned is None:
        return None
    parsed = urlsplit(cleaned)
    if parsed.netloc.lower().endswith("linkedin.com") and parsed.netloc.lower() != "www.linkedin.com":
        parsed = parsed._replace(netloc="www.linkedin.com")
    return urlunsplit(parsed).rstrip("/") or None


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
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class LinkedInAdapter(HttpAdapter):
    source_id = "linkedin"
    market = "global_remote"
    rate_limit = RateLimit(3.0)
    # No boards table entry gets a LinkedIn fetch: refs come from preferences,
    # generated fresh by `board_refs` every run. See SourceAdapter.generates_refs.
    generates_refs = True
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
        # Set by `fetch`, read by `was_truncated` right after. `sync.fetch_pass`
        # calls the latter once per ref, same shape as `still_fetching` - the
        # flag belongs to the envelope, not to the payload it wraps, so it
        # never travels inside the dict `fetch` returns.
        self._truncated = False

    def board_refs(self, prefs: preferences_module.Preferences) -> list[BoardRef]:
        """One search per title and location. LinkedIn has no boards to seed."""
        locations = prefs.locations or [""]
        return [
            BoardRef(provider=self.source_id, token=f"{title}|{location}", market=self.market)
            for title in prefs.titles
            for location in locations
        ]

    def still_fetching(self) -> bool:
        """False once the guard refuses everything: the remaining refs cost a
        rate-limit sleep each and return an empty envelope regardless."""
        return self.guard is None or self.guard.ready()

    def was_truncated(self) -> bool:
        """Whether the fetch just made ended before it saw the whole listing.

        Read by `sync.fetch_pass` right after `fetch()` returns, the same way
        it reads `still_fetching`, so the flag lands on the envelope rather
        than inside `payload` - `payload` is a third-party response body on
        several other adapters, and a bare `truncated` key there would
        collide with anything upstream ever happens to name the same way.
        """
        return self._truncated

    # --- fetch ------------------------------------------------------------

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """Page the search, then fetch details for the ids the corpus doesn't have.

        Never raises on an HTTP problem: a 429 or 403 is recorded on the guard
        and ends the pass early, same as any other non-200. Only a config-less
        adapter (no guard at all) returns immediately - that shape exists for
        `normalize`-only tests and is never how a real run constructs this.

        A pass that ended early leaves `was_truncated()` true afterwards.
        Without it an empty envelope is indistinguishable from "this search
        genuinely returned nothing", and `deactivate_missing` reads that as
        every job of this ref having vanished: two refused runs and they all
        go inactive. With 20 refs against a 400/day budget the tail refs are
        refused every single run, so that is the steady state rather than an
        edge case.
        """
        empty: dict[str, Any] = {"cards": [], "details": {}, "posters": {}}
        if self.guard is None:
            self._truncated = False
            return empty

        try:
            prefs, _ = preferences_module.load(self.config)
        except Exception:
            # A malformed filters.yaml is a config problem, not a fetch problem.
            # This source is never allowed to fail a run over it - but it did not
            # learn that this ref has no jobs either, so the envelope is truncated.
            log.warning("linkedin fetch for %r skipped: preferences could not be read", ref.token)
            self._truncated = True
            return empty
        title, _, location = ref.token.partition("|")

        truncated = False

        def refused(pass_name: str) -> bool:
            """Whether the guard will not allow another request, and says so.

            A refusal that logs nothing and shows nowhere is how a starved ref
            loses its jobs quietly, so every one of them is stated out loud.
            """
            nonlocal truncated
            if self.guard.ready():
                return False
            reason = self.guard.refusal() or "a cooldown longer than it is worth waiting for"
            truncated = True
            log.warning("linkedin %s for %r stopped: %s", pass_name, ref.token, reason)
            return True

        def stopped(pass_name: str, why: str) -> None:
            nonlocal truncated
            truncated = True
            log.warning("linkedin %s for %r stopped: %s", pass_name, ref.token, why)

        cards: list[str] = []
        page = 0
        page_count = self.PAGE_SIZE  # primes the loop for a first request
        while page_count == self.PAGE_SIZE and page < self.MAX_PAGES and not refused("search"):
            params = query.search_params(title, location or None, prefs, start=page * self.PAGE_SIZE)
            self.guard.spend()
            try:
                response = client.get(query.SEARCH_URL, params=params, headers=_HEADERS)
            except httpx.HTTPError as error:
                stopped("search", f"{type(error).__name__}: {error}")
                break
            if response.status_code == 429:
                # Not the end of the pass: the loop condition waits out a short
                # cooldown and asks for the same page again, or stops if the
                # cooldown is long or the breaker has tripped.
                self.guard.record_429(_retry_after(response))
                log.warning("linkedin search for %r got a 429; waiting to retry", ref.token)
                continue
            if response.status_code == 403:
                self.guard.record_403()
                stopped("search", "403 from LinkedIn")
                break
            if response.status_code != 200:
                stopped("search", f"HTTP {response.status_code} from LinkedIn")
                break
            self.guard.record_ok()
            html = response.text
            cards.append(html)
            page_count = len(_cards(html))
            page += 1
            # Plain `allow()`, not `refused()`: a refusal here is logged by the
            # loop condition on the next turn rather than twice.
            if page_count == self.PAGE_SIZE and page < self.MAX_PAGES and self.guard.allow():
                time.sleep(self.guard.delay())

        details: dict[str, str] = {}
        # A detail page is a request against a limit of roughly ten a minute,
        # and a card whose title stage 1 will drop is not worth one: the card
        # is still stored, only its description is not fetched.
        from jobhunt import board_scope

        wanted = board_scope.title_filter(self.config)
        titles = _card_titles(cards)
        pending = [
            job_id for job_id in _job_ids(cards)
            if job_id not in self.known_ids and wanted(titles.get(job_id))
        ]
        while pending:
            job_id = pending[0]
            if refused("detail fetch"):
                break
            # Paced like the search pages, including before this first detail
            # request: it follows the last search page and is otherwise the
            # one request in this adapter with no gap before it.
            time.sleep(self.guard.delay())
            self.guard.spend()
            try:
                response = client.get(query.DETAIL_URL.format(job_id=job_id), headers=_HEADERS)
            except httpx.HTTPError:
                pending.pop(0)
                continue
            if response.status_code == 429:
                # The same id is asked for again once `refused` has waited out
                # the cooldown: a job stored without its description is never
                # fetched again, since later runs skip known ids.
                self.guard.record_429(_retry_after(response))
                log.warning("linkedin detail fetch for %r got a 429; waiting to retry", ref.token)
                continue
            if response.status_code == 403:
                self.guard.record_403()
                stopped("detail fetch", "403 from LinkedIn")
                break
            pending.pop(0)
            if response.status_code != 200:
                continue
            self.guard.record_ok()
            details[job_id] = response.text

        # Reserved for a future pass: LinkedIn's hiring-team block is already
        # carried through via the detail HTML in `details`, so nothing needs
        # to be attached separately here yet. Kept in the envelope shape so
        # `normalize` and any later caller don't have to special-case its
        # absence.
        self._truncated = truncated
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

        # The card's href is regional and slugged (e.g. https://de.linkedin.com/jobs/view/
        # back-end-developer-at-hartleyco-4458072484): fine to click through on, but two
        # probes from different regions would otherwise record two different URLs for the
        # same posting. `external_id` is always present (a card without one is already
        # skipped above), so the canonical form never needs a fallback.
        source_url = f"https://www.linkedin.com/jobs/view/{external_id}"
        link_el = card.select_one("a.base-card__full-link")
        apply_url = _clean_url(link_el.get("href")) if link_el else None
        if apply_url is None:
            apply_url = source_url

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
            apply_url=apply_url,
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
        if desc_el is not None:
            # LinkedIn renders "Show more"/"Show less" as real <button> text inside the
            # description subtree, not CSS-hidden markup - left in place they read as the
            # tail of the JD itself and TRUNCATION_MARKERS mistakes a complete posting for
            # a clipped one. Decomposed, never string-replaced: a JD that legitimately says
            # "show more" in its own prose must survive untouched.
            for button in desc_el.select(".show-more-less-html__button"):
                button.decompose()
        description_html = str(desc_el) if desc_el else None
        description_text = norm.html_to_text(description_html) if description_html else None

        poster_name: str | None = None
        poster_profile_url: str | None = None

        # `message-the-recruiter` is the guest detail page's real markup - the
        # only variant this adapter ever fetches, since the guest endpoint is
        # all `fetch` is able to reach. `hirer-card__hirer-information` below
        # is the authenticated shape; kept as a fallback in case LinkedIn ever
        # serves it here, but it has never actually been observed on a guest
        # fetch.
        recruiter = soup.select_one("div.message-the-recruiter")
        if recruiter is not None:
            link = recruiter.select_one("a[href*='/in/']") or recruiter.select_one("a[href]")
            title_el = recruiter.select_one(".base-main-card__title")
            if title_el is not None:
                poster_name = title_el.get_text(strip=True) or None
            elif link is not None:
                poster_name = link.get_text(strip=True) or None
            if link is not None:
                poster_profile_url = _clean_profile_url(link.get("href"))

        if poster_name is None and poster_profile_url is None:
            hirer = soup.select_one("div.hirer-card__hirer-information")
            if hirer is not None:
                link = hirer.select_one("a[href]")
                if link is not None:
                    poster_profile_url = _clean_profile_url(link.get("href"))
                    poster_name = link.get_text(strip=True) or None

        return description_html, description_text, poster_name, poster_profile_url
