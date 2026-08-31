"""What keeps the LinkedIn adapter from becoming a problem.

Three separate refusals, because they fail differently. The budget bounds a
day's requests and persists per calendar day, so a run late in the day picks
up where earlier runs left off rather than getting a fresh allowance. The
cooldown answers a 429 with patience that outlives the process — persisted,
because a restart that resumes hammering the instant a 429 lands is exactly
how a polite crawler becomes a banned one. The breaker ends a run that is
clearly unwelcome (repeated 429s, or any 403) instead of retrying a hundred
times; it is per-run and lives in memory only, since there is nothing to
survive a restart for — the next run starts hopeful and earns its own trip.

Deliberately absent: proxies and user-agent rotation. Those evade a limit
rather than respect it, and the account they would put at risk is the one
that sends messages on the user's behalf.
"""
from __future__ import annotations

import datetime as dt
import random

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import utcnow
from jobhunt.db.session import session_scope

DAILY_BUDGET = 400
MAX_COOLDOWN_SECONDS = 3600
BREAKER_AFTER = 3
BASE_DELAY = 3.0
JITTER = 1.0

_SPENT_KEY = "linkedin_requests_{day}"
_COOLDOWN_KEY = "linkedin_cooldown_until"


class CrawlGuard:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._streak = 0
        self.tripped = False

    def allow(self, *, now: dt.datetime | None = None) -> bool:
        now = now or utcnow()
        if self.tripped:
            return False
        until = self.cooling_until()
        if until is not None and now < until:
            return False
        return self._spent(now) < DAILY_BUDGET

    def spend(self, n: int = 1, *, now: dt.datetime | None = None) -> None:
        now = now or utcnow()
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            current = int(store.meta_get(session, key, default="0") or 0)
            store.meta_set(session, key, str(current + n))

    def record_ok(self) -> None:
        self._streak = 0

    def record_429(self, retry_after: float | None, *, now: dt.datetime | None = None) -> None:
        now = now or utcnow()
        self._streak += 1
        if self._streak >= BREAKER_AFTER:
            self.tripped = True
        # Exponential in the streak, so a source that keeps refusing is left alone
        # for longer each time rather than probed at a fixed rhythm.
        if retry_after:
            seconds = float(retry_after)
        else:
            seconds = float(min(60 * (2 ** (self._streak - 1)), MAX_COOLDOWN_SECONDS))
        seconds = min(seconds, float(MAX_COOLDOWN_SECONDS))
        with session_scope(self.config.db_path) as session:
            store.meta_set(session, _COOLDOWN_KEY, (now + dt.timedelta(seconds=seconds)).isoformat())

    def record_403(self) -> None:
        """A 403 is not a rate limit. Nothing about waiting fixes it."""
        self.tripped = True

    def cooling_until(self) -> dt.datetime | None:
        with session_scope(self.config.db_path) as session:
            raw = store.meta_get(session, _COOLDOWN_KEY)
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def delay(self) -> float:
        return BASE_DELAY + random.uniform(-JITTER, JITTER)

    def _spent(self, now: dt.datetime) -> int:
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            return int(store.meta_get(session, key, default="0") or 0)
