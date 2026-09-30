"""What keeps the LinkedIn adapter from becoming a problem.

Everything here is about LinkedIn's logged-out guest pages, read with plain
HTTP and no session. Nothing signs in, so what an over-eager crawl puts at
risk is this machine's IP address being throttled or blocked for a while - not
the user's LinkedIn account. The account only acts through Unipile, for
outreach, which is a separate path with its own limits.

LinkedIn publishes no rate limit for these pages. So the rules that react to
what LinkedIn actually says are kept, and the ones that were only guesses are
off until they can be set from measurement:

- The daily budget. `DAILY_BUDGET` is None, so no cap applies. Set it to a
  number and the count persists per calendar day, so a run late in the day
  picks up where earlier runs left off.
- The pause between requests. `BASE_DELAY` and `JITTER` are 0, so requests go
  out back to back; `delay()` is still called before each one, so a pause can
  be set again without touching the callers.

Requests are counted per day and timed per run, and every 429 or 403 is logged
with how many requests preceded it - today, this run, and in the last minute
and ten minutes (`refusal_history`) - so both limits can be set from where
LinkedIn really starts refusing.
- The cooldown answers a 429 with patience that outlives the process -
  persisted, because a restart that resumes hammering the instant a 429 lands
  is exactly how a polite crawler becomes a blocked one.
- The breaker ends a run that is clearly unwelcome (repeated 429s, or any 403)
  instead of retrying a hundred times. It is per-run and lives in memory only,
  since there is nothing to survive a restart for - the next run starts
  hopeful and earns its own trip.


Deliberately absent: proxies and user-agent rotation. Those evade a limit
rather than respect it.
"""
from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import utcnow
from jobhunt.db.session import session_scope

# None: no daily cap. See the module docstring.
DAILY_BUDGET: int | None = None
MAX_COOLDOWN_SECONDS = 3600
# A 403 is the loudest refusal this source gives, so it buys the longest pause
# the cooldown allows rather than a graduated one.
FORBIDDEN_COOLDOWN_SECONDS = MAX_COOLDOWN_SECONDS
BREAKER_AFTER = 3
# Seconds before each request, and the random spread around it. Both 0: no
# pause. See the module docstring.
BASE_DELAY = 0.0
JITTER = 0.0

_SPENT_KEY = "linkedin_requests_{day}"
_COOLDOWN_KEY = "linkedin_cooldown_until"
_REFUSALS_KEY = "linkedin_refusals"
# Enough refusals to see a pattern; old ones are dropped past this.
REFUSALS_KEPT = 100


class CrawlGuard:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._streak = 0
        self.tripped = False
        self.spent_this_run = 0
        # When each request of this run went out, for the rate at a refusal.
        self._sent_at: list[dt.datetime] = []

    def allow(self, *, now: dt.datetime | None = None) -> bool:
        return self.refusal(now=now) is None

    def refusal(self, *, now: dt.datetime | None = None) -> str | None:
        """Why the next request must not go out, phrased for a log line.

        The caller needs the reason, not just the verdict: a refusal that says
        nothing looks exactly like a search that legitimately found no jobs, and
        the pipeline then retires the jobs this ref actually still has.
        """
        now = now or utcnow()
        if self.tripped:
            return "the circuit breaker tripped for this run"
        until = self.cooling_until()
        if until is not None and now < until:
            return f"a cooldown is in force until {until.isoformat()}"
        spent = self._spent(now)
        if DAILY_BUDGET is not None and spent >= DAILY_BUDGET:
            return f"today's budget of {DAILY_BUDGET} requests is spent ({spent})"
        return None

    def spend(self, n: int = 1, *, now: dt.datetime | None = None) -> None:
        now = now or utcnow()
        self.spent_this_run += n
        self._sent_at.extend([now] * n)
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            current = int(store.meta_get(session, key, default="0") or 0)
            store.meta_set(session, key, str(current + n))

    def record_ok(self) -> None:
        self._streak = 0

    def record_429(self, retry_after: float | None, *, now: dt.datetime | None = None) -> None:
        now = now or utcnow()
        self._note_refusal(429, retry_after, now)
        self._streak += 1
        if self._streak >= BREAKER_AFTER:
            self.tripped = True
        # Exponential in the streak, so a source that keeps refusing is left alone
        # for longer each time rather than probed at a fixed rhythm.
        if retry_after is not None:
            seconds = float(retry_after)
        else:
            seconds = float(min(60 * (2 ** (self._streak - 1)), MAX_COOLDOWN_SECONDS))
        seconds = min(seconds, float(MAX_COOLDOWN_SECONDS))
        with session_scope(self.config.db_path) as session:
            store.meta_set(session, _COOLDOWN_KEY, (now + dt.timedelta(seconds=seconds)).isoformat())

    def record_403(self, *, now: dt.datetime | None = None) -> None:
        """A 403 is not a rate limit, but it is not an invitation to retry either.

        The breaker only lasts this run, and the next run starts hopeful - which
        for a source that just refused us outright means hammering it again
        minutes later. The cooldown is persisted so that pause outlives the
        process, the same way a 429's does.
        """
        now = now or utcnow()
        self._note_refusal(403, None, now)
        self.tripped = True
        with session_scope(self.config.db_path) as session:
            store.meta_set(
                session,
                _COOLDOWN_KEY,
                (now + dt.timedelta(seconds=FORBIDDEN_COOLDOWN_SECONDS)).isoformat(),
            )

    def _note_refusal(self, status: int, retry_after: float | None, now: dt.datetime) -> None:
        """Log a refusal against how much had been asked, today and this run."""
        event = {
            "at": now.isoformat(timespec="seconds"),
            "status": status,
            "retry_after": retry_after,
            "spent_today": self._spent(now),
            "spent_this_run": self.spent_this_run,
            "last_minute": self._sent_since(now, 60),
            "last_10_minutes": self._sent_since(now, 600),
            "run_minutes": (
                round((now - self._sent_at[0]).total_seconds() / 60, 1) if self._sent_at else 0.0
            ),
            "pause_seconds": BASE_DELAY,
        }
        with session_scope(self.config.db_path) as session:
            history = _load(store.meta_get(session, _REFUSALS_KEY))
            history = [*history, event][-REFUSALS_KEPT:]
            store.meta_set(session, _REFUSALS_KEY, json.dumps(history))

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
        return max(0.0, BASE_DELAY + random.uniform(-JITTER, JITTER))

    def _sent_since(self, now: dt.datetime, seconds: float) -> int:
        cutoff = now - dt.timedelta(seconds=seconds)
        return sum(1 for sent in self._sent_at if sent >= cutoff)

    def _spent(self, now: dt.datetime) -> int:
        with session_scope(self.config.db_path) as session:
            key = _SPENT_KEY.format(day=now.date().isoformat())
            return int(store.meta_get(session, key, default="0") or 0)


def refusal_history(config: Config) -> list[dict[str, Any]]:
    """Every 429 and 403 logged so far, oldest first, with the request counts
    at the time. What a daily cap should be set from."""
    with session_scope(config.db_path) as session:
        return _load(store.meta_get(session, _REFUSALS_KEY))


def _load(raw: str | None) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []
