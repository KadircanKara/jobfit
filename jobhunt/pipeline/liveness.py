"""Check that a posting is still open before it goes on a shortlist.

A job only leaves the corpus when a full listing of its board stops carrying
it, twice. LinkedIn has no full listing - a search returns its top results, so
a job missing from one proves nothing - and a board is only re-read every week.
Either way a job can close well before the corpus notices, and a shortlist is
the one place that must not show it.

So the posting's own page is read just before the job is shown:

- a 404 or 410 means it is gone;
- a page that says so ("No longer accepting applications") means it is closed;
- a redirect that lands somewhere without the job's id (a board's front page,
  Greenhouse's `?error=true`) means the posting no longer exists.

A closed job is marked inactive, so nothing surfaces it again until a listing
carries it anew. An open one is stamped, and not read again for a day.

When a page cannot be read, the job is kept on the word of the listing - an
outage on one board must not empty the shortlist - except on LinkedIn: that
is the source this check exists for, so a LinkedIn job the rate guard will not
let us look at is held back rather than shown unchecked.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
import urllib.parse

import httpx

from jobhunt.config import Config
from jobhunt.db.models import Job, utcnow
from jobhunt.db.session import session_scope
from jobhunt.pipeline import jd_fetch
from jobhunt.pipeline import normalize as norm
from jobhunt.sources import linkedin_query
from jobhunt.sources.linkedin_guard import CrawlGuard

TIMEOUT = 10.0
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
# An open posting is not read again for this long. A run a few hours after the
# last one should not spend LinkedIn's budget re-reading the same shortlist.
RECHECK_AFTER = dt.timedelta(hours=24)

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"

# What a closed posting's page says, lowercased. Kept to whole phrases a job
# description would not use about itself, and matched against the visible text
# only: a single-page app ships every message it might ever show in a script.
CLOSED_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "this job is no longer open",
    "this position is no longer available",
    "this position has been filled",
    "this job has expired",
    "this job posting has expired",
    "this posting has closed",
    "the job you are looking for is no longer",
    "the position you are looking for is no longer",
)


@dataclasses.dataclass
class Result:
    closed: set[int] = dataclasses.field(default_factory=set)
    # LinkedIn jobs the guard would not let us check. Not marked anything:
    # held back from this shortlist only.
    unchecked: set[int] = dataclasses.field(default_factory=set)
    confirmed_open: int = 0

    @property
    def hidden(self) -> set[int]:
        return self.closed | self.unchecked


def verify(
    config: Config,
    job_ids: list[int],
    client: httpx.Client | None = None,
    guard: CrawlGuard | None = None,
) -> Result:
    """Read each job's posting page, and record which ones have closed."""
    result = Result()
    now = utcnow()
    targets: list[tuple[int, str, str, str]] = []
    with session_scope(config.db_path) as session:
        for job_id in job_ids:
            job = session.get(Job, job_id)
            if job is None or not job.is_active:
                continue
            if job.open_checked_at and now - job.open_checked_at < RECHECK_AFTER:
                continue
            url = job.source_url or job.apply_url
            if job.source != "linkedin" and not url:
                continue
            targets.append((job_id, job.source, job.external_id, url or ""))
    if not targets:
        return result

    own = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": config.get("http", "user_agent"), "Accept": "text/html"},
    )
    guard = guard or CrawlGuard(config)
    linkedin_blocked = False
    try:
        for job_id, source, external_id, url in targets:
            if source == "linkedin":
                if linkedin_blocked or not guard.allow():
                    linkedin_blocked = True
                    result.unchecked.add(job_id)
                    continue
                state, linkedin_blocked = _linkedin_state(client, guard, external_id)
                if state == UNKNOWN:
                    result.unchecked.add(job_id)
                    continue
            else:
                state = _page_state(client, url, external_id)
                if state == UNKNOWN:
                    continue
            _record(config, job_id, state)
            if state == CLOSED:
                result.closed.add(job_id)
            else:
                result.confirmed_open += 1
    finally:
        if own:
            client.close()
    return result


def _record(config: Config, job_id: int, state: str) -> None:
    with session_scope(config.db_path) as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if state == CLOSED:
            job.is_active = False
        else:
            job.open_checked_at = utcnow()


def _linkedin_state(client: httpx.Client, guard: CrawlGuard, external_id: str) -> tuple[str, bool]:
    """(state, whether LinkedIn has stopped answering for this pass)."""
    # Paced like the adapter's own detail fetches, first request included.
    time.sleep(guard.delay())
    guard.spend()
    try:
        response = client.get(
            linkedin_query.DETAIL_URL.format(job_id=external_id),
            headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
        )
    except httpx.HTTPError:
        return UNKNOWN, False
    if response.status_code == 429:
        guard.record_429(None)
        return UNKNOWN, True
    if response.status_code == 403:
        guard.record_403()
        return UNKNOWN, True
    if response.status_code in (404, 410):
        guard.record_ok()
        return CLOSED, False
    if response.status_code != 200:
        return UNKNOWN, False
    guard.record_ok()
    return (CLOSED if says_closed(response.text) else OPEN), False


def _page_state(client: httpx.Client, url: str, external_id: str) -> str:
    """Follow the posting URL a few hops, checking each, and judge where it lands."""
    start = url
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            jd_fetch.check(url)
        except jd_fetch.Unsafe:
            return UNKNOWN
        try:
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return UNKNOWN
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status_code in (404, 410):
                    return CLOSED
                if response.status_code != 200:
                    return UNKNOWN
                if url != start and _left_the_posting(start, url, external_id):
                    return CLOSED
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                text = body.decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError:
            return UNKNOWN
        return CLOSED if says_closed(text) else OPEN
    return UNKNOWN


def _left_the_posting(start: str, final: str, external_id: str) -> bool:
    """Whether a redirect took us off the posting: to an error page, or to a
    page that no longer names the job the original URL named."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
    if query.get("error") == ["true"]:
        return True
    ident = (external_id or "").strip().lower()
    return bool(ident) and ident in start.lower() and ident not in final.lower()


def says_closed(html: str) -> bool:
    text = " ".join(norm.html_to_text(html).lower().split())
    return any(phrase in text for phrase in CLOSED_PHRASES)
