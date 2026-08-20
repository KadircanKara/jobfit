"""Exchange rates for the salary rule.

A run takes one snapshot and compares every job against it. Refetching mid-run
would mean two identical postings in different currencies could be judged
differently depending on when the scraper reached them.

When the network is unavailable the last snapshot is used and labelled with its
age. When there has never been one, the salary rule is skipped for that run and
said so out loud — filtering on numbers we do not have would quietly drop jobs
the user asked for.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from collections.abc import Callable

import httpx

from jobhunt.config import Config

# Free, no key, no attribution requirement.
ENDPOINT = "https://open.er-api.com/v6/latest/{base}"
BASE = "USD"
DEFAULT_MAX_AGE_HOURS = 12.0

Fetcher = Callable[[str], dict[str, float]]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    rates: dict[str, float]
    fetched_at: dt.datetime
    source: str  # live | cache | none

    @property
    def usable(self) -> bool:
        return bool(self.rates)

    @property
    def age_hours(self) -> float:
        return (dt.datetime.now(dt.UTC) - self.fetched_at).total_seconds() / 3600

    def convert(self, amount: float, frm: str, to: str) -> int | None:
        """`amount` in `frm`, expressed in `to`. None when it cannot be known."""
        if not self.usable:
            return None
        frm, to = frm.upper(), to.upper()
        if frm == to:
            return round(amount)
        if frm not in self.rates or to not in self.rates:
            return None
        return round(amount / self.rates[frm] * self.rates[to])

    def as_dict(self) -> dict:
        return {
            "rates": self.rates,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
            "usable": self.usable,
        }


def cache_path(config: Config) -> pathlib.Path:
    return pathlib.Path(config.data_dir) / "fx.json"


def _http_fetch(base: str) -> dict[str, float]:
    response = httpx.get(ENDPOINT.format(base=base), timeout=15.0)
    response.raise_for_status()
    payload = response.json()
    rates = payload.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise ValueError("rates missing from the response")
    return {str(code): float(value) for code, value in rates.items()}


def _read_cache(config: Config) -> Snapshot | None:
    path = cache_path(config)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(
            rates={str(k): float(v) for k, v in payload["rates"].items()},
            fetched_at=dt.datetime.fromisoformat(payload["fetched_at"]),
            source="cache",
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # A half-written file is the same as no file. Rewriting it is the
        # next successful fetch's job.
        return None


def _write_cache(config: Config, snapshot: Snapshot) -> None:
    path = cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rates": snapshot.rates, "fetched_at": snapshot.fetched_at.isoformat()}),
        encoding="utf-8",
    )


def load(
    config: Config,
    *,
    fetch: Fetcher | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> Snapshot:
    """The snapshot a run should use, freshest first."""
    cached = _read_cache(config)
    if cached and cached.age_hours <= max_age_hours:
        return cached

    try:
        rates = (fetch or _http_fetch)(BASE)
        live = Snapshot(rates=rates, fetched_at=dt.datetime.now(dt.UTC), source="live")
        _write_cache(config, live)
        return live
    except Exception:
        if cached:
            return cached
        return Snapshot(rates={}, fetched_at=dt.datetime.now(dt.UTC), source="none")
