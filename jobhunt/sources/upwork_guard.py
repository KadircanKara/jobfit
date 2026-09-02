"""What keeps Upwork fetching from eating a session's time and tokens.

This is not a spend limit. Upwork's search and read operations cost no
Connects and no money, so there is nothing here protecting an account or a
wallet. What is scarce is the caller: every ref this module bounds is a whole
`claude -p` agent turn, and each of those costs latency, tokens, and runs
against the MCP's own rate limits. The budget exists so one day's fetching
cannot quietly turn into dozens of agent turns.

The daily counter persists per calendar day, the same way the LinkedIn guard's
does, so a run late in the day picks up where earlier runs left off rather
than getting a fresh allowance. The breaker is per-run and lives in memory
only - a run that is clearly going wrong (repeated failures) should stop
retrying rather than burn through the budget on turns that are not landing,
but the next run starts hopeful and earns its own trip.
"""
from __future__ import annotations

import datetime as dt

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import utcnow
from jobhunt.db.session import session_scope

# Attempts, not refs: `fetch` spends a unit on each of its two attempts, so a
# day of retries is 6 refs, not 12. Counting attempts is the point - the scarce
# thing is the `claude -p` turn, and a retry is a whole extra turn with a whole
# extra live search behind it. The name is kept because the daily meta key it
# writes is already in the database under it; read it as "ref attempts".
DAILY_REFS = 12
BREAKER_AFTER = 3

_SPENT_KEY = "upwork_refs_{day}"


class FetchBudget:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._streak = 0
        self.tripped = False

    def allow(self, *, now: dt.datetime | None = None) -> bool:
        return self.refusal(now=now) is None

    def refusal(self, *, now: dt.datetime | None = None) -> str | None:
        """Why the next ref must not go out, phrased for a log line.

        The caller needs the reason, not just the verdict, so it can tell a
        refused fetch apart from a fetch that legitimately found nothing.
        """
        now = now or utcnow()
        if self.tripped:
            return "the circuit breaker tripped for this run"
        spent = self._spent(now)
        if spent >= DAILY_REFS:
            return f"today's budget of {DAILY_REFS} fetch attempts is spent ({spent})"
        return None

    def spend(self, n: int = 1, *, now: dt.datetime | None = None) -> None:
        now = now or utcnow()
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            current = int(store.meta_get(session, key, default="0") or 0)
            store.meta_set(session, key, str(current + n))

    def record_ok(self) -> None:
        self._streak = 0

    def record_failure(self) -> None:
        self._streak += 1
        if self._streak >= BREAKER_AFTER:
            self.tripped = True

    def _spent(self, now: dt.datetime) -> int:
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            return int(store.meta_get(session, key, default="0") or 0)
