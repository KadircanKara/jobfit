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


@runtime_checkable
class SourceAdapter(Protocol):
    source_id: str
    market: str
    rate_limit: RateLimit

    def discover(self) -> Iterator[BoardRef]:
        """Yield the boards or queries to fetch."""
        ...

    def fetch(self, ref: BoardRef, client: httpx.Client) -> Any:
        """Return the raw payload for one ref. Must not transform it."""
        ...

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        """Raw payload -> canonical records. Pure function, no I/O."""
        ...


class HttpAdapter:
    """Shared plumbing for HTTP-backed adapters. Subclasses implement the protocol."""

    source_id: str = "base"
    market: str = "global_remote"
    rate_limit: RateLimit = RateLimit(1.0)

    def __init__(self, refs: list[BoardRef] | None = None) -> None:
        self._refs = refs or []

    def discover(self) -> Iterator[BoardRef]:
        yield from self._refs

    @staticmethod
    def _get_json(client: httpx.Client, url: str) -> Any:
        response = client.get(url)
        response.raise_for_status()
        return response.json()
