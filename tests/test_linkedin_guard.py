import datetime as dt

import pytest

from jobhunt.db.models import utcnow
from jobhunt.sources import linkedin_guard
from jobhunt.sources.linkedin_guard import CrawlGuard, refusal_history

DAILY_BUDGET = 400


@pytest.fixture
def capped(monkeypatch):
    """The cap is off by default; the tests below are about how it behaves when set."""
    monkeypatch.setattr(linkedin_guard, "DAILY_BUDGET", DAILY_BUDGET)


def test_a_fresh_day_allows_requests(cfg) -> None:
    assert CrawlGuard(cfg).allow() is True


def test_there_is_no_daily_cap_by_default(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.spend(10_000)
    assert guard.allow() is True


def test_a_429_is_logged_against_the_requests_made_so_far(cfg) -> None:
    guard = CrawlGuard(cfg)
    guard.spend(37)
    guard.record_429(retry_after=120.0)
    CrawlGuard(cfg).record_403()

    history = refusal_history(cfg)
    assert [event["status"] for event in history] == [429, 403]
    assert history[0]["spent_today"] == 37 and history[0]["spent_this_run"] == 37
    assert history[0]["retry_after"] == 120.0
    assert history[1]["spent_this_run"] == 0


def test_the_refusal_log_keeps_only_the_latest(cfg, monkeypatch) -> None:
    monkeypatch.setattr(linkedin_guard, "REFUSALS_KEPT", 3)
    guard = CrawlGuard(cfg)
    for _ in range(5):
        guard.spend(1)
        guard.record_429(retry_after=None)
    assert [event["spent_today"] for event in refusal_history(cfg)] == [3, 4, 5]


def test_the_daily_budget_is_finite(cfg, capped) -> None:
    guard = CrawlGuard(cfg)
    guard.spend(DAILY_BUDGET)
    assert guard.allow() is False


def test_the_budget_survives_a_restart(cfg, capped) -> None:
    CrawlGuard(cfg).spend(DAILY_BUDGET)
    assert CrawlGuard(cfg).allow() is False


def test_the_budget_resets_the_next_day(cfg, capped) -> None:
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


def test_there_is_no_pause_between_requests_by_default(cfg) -> None:
    assert CrawlGuard(cfg).delay() == 0.0


def test_a_pause_set_again_is_jittered_and_never_negative(cfg, monkeypatch) -> None:
    monkeypatch.setattr(linkedin_guard, "BASE_DELAY", 3.0)
    monkeypatch.setattr(linkedin_guard, "JITTER", 1.0)
    delays = {round(CrawlGuard(cfg).delay(), 3) for _ in range(50)}
    assert len(delays) > 1
    assert all(2.0 <= d <= 4.0 for d in delays)
    monkeypatch.setattr(linkedin_guard, "BASE_DELAY", 0.5)
    assert all(CrawlGuard(cfg).delay() >= 0 for _ in range(50))


def test_a_refusal_records_the_request_rate_before_it(cfg) -> None:
    guard = CrawlGuard(cfg)
    start = utcnow()
    guard.spend(5, now=start - dt.timedelta(minutes=9))
    guard.spend(3, now=start - dt.timedelta(seconds=30))
    guard.record_429(retry_after=None, now=start)

    event = refusal_history(cfg)[-1]
    assert event["last_minute"] == 3
    assert event["last_10_minutes"] == 8
    assert event["run_minutes"] == 9.0
    assert event["pause_seconds"] == 0.0

def test_a_hostile_retry_after_is_clamped_to_the_cap(cfg) -> None:
    from jobhunt.sources.linkedin_guard import MAX_COOLDOWN_SECONDS

    guard = CrawlGuard(cfg)
    before = utcnow()
    guard.record_429(retry_after=999999)
    until = guard.cooling_until()
    assert until is not None
    assert until <= before + dt.timedelta(seconds=MAX_COOLDOWN_SECONDS + 2)


def test_a_zero_retry_after_is_honoured_not_backed_off(cfg) -> None:
    guard = CrawlGuard(cfg)
    before = utcnow()
    guard.record_429(retry_after=0.0)
    until = guard.cooling_until()
    assert until is not None
    assert until <= before + dt.timedelta(seconds=2)
