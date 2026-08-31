import datetime as dt

from jobhunt.db.models import utcnow
from jobhunt.sources.linkedin_guard import DAILY_BUDGET, CrawlGuard


def test_a_fresh_day_allows_requests(cfg) -> None:
    assert CrawlGuard(cfg).allow() is True


def test_the_daily_budget_is_finite(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.spend(DAILY_BUDGET)
    assert guard.allow() is False


def test_the_budget_survives_a_restart(cfg) -> None:
    CrawlGuard(cfg).spend(DAILY_BUDGET)
    assert CrawlGuard(cfg).allow() is False


def test_the_budget_resets_the_next_day(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.spend(DAILY_BUDGET)
    tomorrow = utcnow() + dt.timedelta(days=1)
    assert guard.allow(now=tomorrow) is True


def test_a_429_starts_a_cooldown(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.record_429(retry_after=None)
    assert guard.allow() is False


def test_a_cooldown_honours_retry_after(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.record_429(retry_after=30.0)
    assert guard.allow(now=utcnow() + dt.timedelta(seconds=31)) is True


def test_the_cooldown_survives_a_restart(cfg) -> None:
    CrawlGuard(cfg).record_429(retry_after=600.0)
    assert CrawlGuard(cfg).allow() is False


def test_three_consecutive_429s_trip_the_breaker(cfg) -> None:
    guard = CrawlGuard(cfg)
    for _ in range(3):
        guard.record_429(retry_after=None)
    assert guard.tripped is True


def test_a_success_clears_the_streak(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.record_429(retry_after=None)
    guard.record_429(retry_after=None)
    guard.record_ok()
    guard.record_429(retry_after=None)
    assert guard.tripped is False


def test_a_403_trips_the_breaker_immediately(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.record_403()
    assert guard.tripped is True


def test_the_delay_is_jittered_around_three_seconds(cfg) -> None:
    delays = {round(CrawlGuard(cfg).delay(), 3) for _ in range(50)}
    assert len(delays) > 1
    assert all(2.0 <= d <= 4.0 for d in delays)
