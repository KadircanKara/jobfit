"""The source adapter contract.

Adding a source must never require touching the pipeline. PLAN.md section 8.

Fetch and normalize are separate passes over separate storage: fetch persists raw
payloads to disk and returns nothing the pipeline needs; normalize is a pure
function from a stored payload to a JobPosting. A parser bug costs a re-normalize,
never a re-fetch.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

import httpx


@dataclasses.dataclass(frozen=True)
class RateLimit:
    """Minimum seconds between requests to one source."""

    delay_seconds: float = 1.0

    def sleep(self) -> None:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)


@dataclasses.dataclass(frozen=True)
class BoardRef:
    """One fetchable unit of work: a board, a search, or a listing page."""

    provider: str
    token: str
    market: str = "global_remote"
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def key(self) -> str:
        """Filesystem-safe identity, used to name the raw payload file."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.token)
        return f"{self.provider}__{safe}"


@dataclasses.dataclass
class JobPosting:
    """The canonical normalized record.

    This is what every adapter produces and the only thing the pipeline consumes.
    It is deliberately not the ORM model: normalize() must stay a pure function
    with no session and no database.
    """

    source: str
    external_id: str
    market: str
    title: str
    company_name: str

    company_domain: str | None = None
    location_raw: str | None = None
    country: str | None = None
    city: str | None = None
    remote_type: str = "unknown"
    employment_type: str | None = None

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_is_stated: bool = False

    description_html: str | None = None
    description_text: str | None = None
    description_md: str | None = None
    description_lang: str | None = None

    jd_completeness: str = "none"  # full | snippet | none
    jd_source: str | None = None

    posted_at: dt.datetime | None = None
    apply_url: str | None = None
    source_url: str | None = None

    departments: list[str] = dataclasses.field(default_factory=list)
    raw_ref: str | None = None  # path of the raw payload this came from

    # LinkedIn is the only source that publishes who posted a job. Optional on
    # every other adapter's postings, which simply never set them.
    poster_name: str | None = None
    poster_profile_url: str | None = None

    # Upwork is the only source that says anything about who is paying. None
    # everywhere else, and None means unknown rather than zero - the rules that
    # read these must be able to tell "never hired" from "never asked".
    client_verified: bool | None = None
    client_total_spent: float | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    source_id: str
    market: str
    rate_limit: RateLimit
    # Most sources are seeded: their refs are Board rows a crawl strategy
    # discovered once and the sync loop re-fetches on a schedule out of the
    # `boards` table. LinkedIn has no boards to seed - its refs are generated
    # fresh from the user's saved preferences on every run. `sync_source`
    # checks this flag to decide whether to pull refs from `due_boards()` (the
    # seeded case) or from the adapter's own `discover()` (the generated
    # case), so a generated-ref source is never silently starved by a query
    # that only ever finds rows for the seeded kind.
    generates_refs: bool = False

    def discover(self) -> Iterator[BoardRef]:
        """Yield the boards or queries to fetch."""
        ...

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        """Return the raw payload for one ref. Must not transform it."""
        ...

    def still_fetching(self) -> bool:
        """Whether the fetch loop should keep pacing between refs. See HttpAdapter."""
        ...

    def was_truncated(self) -> bool:
        """Whether the fetch just made ended before it saw the whole listing.

        Read once per ref, right after `fetch()` returns - see HttpAdapter.
        """
        ...

    def was_refused(self) -> bool:
        """Whether the fetch just made never went out at all. See HttpAdapter."""
        ...

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        """Raw payload -> canonical records. Pure function, no I/O."""
        ...


class HttpAdapter:
    """Shared plumbing for HTTP-backed adapters. Subclasses implement the protocol."""

    source_id: str = "base"
    market: str = "global_remote"
    rate_limit: RateLimit = RateLimit(1.0)
    # See SourceAdapter.generates_refs above.
    generates_refs: bool = False

    def __init__(self, refs: list[BoardRef] | None = None) -> None:
        self._refs = refs or []

    def still_fetching(self) -> bool:
        """Whether further refs are worth pacing for.

        The fetch loop sleeps between refs to be polite. An adapter that has
        stopped making requests at all - LinkedIn once its crawl guard refuses -
        has nothing to be polite about, and with many refs those sleeps are
        minutes spent between no-op fetches. Everything else keeps fetching until
        it runs out of refs, so the default is simply True.
        """
        return True

    def was_truncated(self) -> bool:
        """Whether the fetch just made ended before it saw the whole listing.

        Every adapter but LinkedIn always sees the whole listing it asked for,
        so the default is simply False.
        """
        return False

    def was_refused(self) -> bool:
        """Whether the fetch just made was refused by a budget before going out.

        Distinct from `was_truncated`: truncated means "we looked and did not
        see everything", refused means "we never looked". Both produce an empty
        payload that would otherwise read as a search that found nothing. Only
        the Upwork adapter has a budget that can refuse, so the default is False.
        """
        return False

    def set_refs(self, refs: list[BoardRef]) -> None:
        """Replace the refs `discover()` yields.

        For a `generates_refs` adapter, the refs depend on preferences the
        constructor cannot see without also owning `config`/`known_ids` wiring
        that has nothing to do with what gets fetched. This lets a caller build
        the adapter once and supply its refs afterward, instead of reaching
        into `_refs` directly or constructing the adapter twice.
        """
        self._refs = refs

    def discover(self) -> Iterator[BoardRef]:
        yield from self._refs

    @staticmethod
    def _get_json(client: httpx.Client, url: str) -> Any:
        response = client.get(url)
        response.raise_for_status()
        return response.json()
