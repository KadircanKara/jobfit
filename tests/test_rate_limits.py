"""A source saying "too many requests" is waited out, never read as a dead
board. No network and no real waiting: adapters are stubs and `time.sleep`
is replaced."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from jobhunt import sync
from jobhunt.db.models import Board, utcnow
from jobhunt.db.session import session_scope
from jobhunt.sources import linkedin_guard, throttle
from jobhunt.sources.linkedin_guard import CrawlGuard


def too_many(retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    request = httpx.Request("GET", "https://api.example.com/board")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


class ThrottledAdapter:
    """Answers 429 for the first `refusals` requests, then serves every board."""

    def __init__(self, refusals: int, retry_after: str | None = "0") -> None:
        self.left = refusals
        self.retry_after = retry_after
        self.calls: list[str] = []
        self.rate_limit = type("Pace", (), {"sleep": lambda self: None, "delay_seconds": 1.0})()

    def still_fetching(self) -> bool:
        return True

    def was_truncated(self) -> bool:
        return False

    def was_refused(self) -> bool:
        return False

    def fetch(self, ref, client):
        self.calls.append(ref.token)
        if self.left:
            self.left -= 1
            raise too_many(self.retry_after)
        return {"jobs": []}


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(throttle, "pause", lambda seconds, should_stop=None: calls.append(seconds) or True)
    return calls


def refs(n: int = 3) -> list[sync.BoardRef]:
    return [sync.BoardRef(provider="personio", token=f"co{i}", market="global_remote") for i in range(n)]


def use(monkeypatch, adapter) -> None:
    factory = type("Factory", (), {"generates_refs": False, "__call__": lambda self: adapter})()
    monkeypatch.setattr(sync.source_registry, "get", lambda name: factory)


# --- job boards ------------------------------------------------------------------


def test_a_429_is_waited_out_and_the_same_board_asked_again(cfg, monkeypatch, slept) -> None:
    adapter = ThrottledAdapter(refusals=1, retry_after="7")
    use(monkeypatch, adapter)
    report = throttle.Report()

    fetched, failed, _ = sync.fetch_pass(cfg, "personio", refs(), "run1", throttled=report)

    assert (fetched, failed) == (3, [])
    assert adapter.calls == ["co0", "co0", "co1", "co2"]
    assert slept == [7.0]
    assert (report.refusals, report.waited, report.left) == (1, 7.0, 0)


def test_without_retry_after_the_wait_doubles(cfg, monkeypatch, slept) -> None:
    use(monkeypatch, ThrottledAdapter(refusals=3, retry_after=None))

    sync.fetch_pass(cfg, "personio", refs(1), "run1")

    assert slept == [30.0, 60.0, 120.0]


def test_a_source_that_keeps_refusing_stops_without_failing_a_board(cfg, monkeypatch, slept) -> None:
    use(monkeypatch, ThrottledAdapter(refusals=100, retry_after=None))
    report = throttle.Report()

    fetched, failed, messages = sync.fetch_pass(cfg, "personio", refs(), "run1", throttled=report)

    assert (fetched, failed) == (0, [])
    assert report.left == 3
    assert sum(slept) <= throttle.MAX_WAIT_PER_SOURCE
    assert "3 boards left for the next run" in messages[-1]


def test_a_retry_after_too_long_to_wait_stops_the_source_at_once(cfg, monkeypatch, slept) -> None:
    use(monkeypatch, ThrottledAdapter(refusals=1, retry_after="3600"))
    report = throttle.Report()

    fetched, failed, _ = sync.fetch_pass(cfg, "personio", refs(), "run1", throttled=report)

    assert (fetched, failed, slept, report.left) == (0, [], [], 3)


def test_every_429_is_logged_against_the_requests_before_it(cfg, monkeypatch, slept) -> None:
    use(monkeypatch, ThrottledAdapter(refusals=1, retry_after="5"))
    sync.fetch_pass(cfg, "personio", refs(), "run1")

    [event] = throttle.history(cfg)
    assert event["source"] == "personio" and event["board"] == "co0"
    assert event["requests_this_run"] == 1
    assert event["retry_after"] == 5.0 and event["wait"] == 5.0
    assert event["pause_seconds"] == 1.0


def test_a_throttled_run_is_degraded_and_kills_no_board(cfg, monkeypatch, slept) -> None:
    with session_scope(cfg.db_path) as session:
        for token in ("co0", "co1"):
            session.add(Board(
                provider="personio", token=token, discovered_via="manual", status="validated", tier="warm",
            ))
    use(monkeypatch, ThrottledAdapter(refusals=100, retry_after="3600"))
    monkeypatch.setattr(
        "jobhunt.board_scope.relevant_boards",
        lambda *args, **kwargs: [
            sync.BoardRef(provider="personio", token=t, market="global_remote") for t in ("co0", "co1")
        ],
    )

    result = sync.sync_source(cfg, "personio")

    assert result.status == "degraded"
    assert (result.errors, result.throttled) == (0, 2)
    with session_scope(cfg.db_path) as session:
        boards = session.query(Board).filter_by(provider="personio").all()
        assert {b.status for b in boards} == {"validated"}
        assert {b.consecutive_errors for b in boards} == {0}


# --- LinkedIn ----------------------------------------------------------------------


class Clock:
    """`utcnow` for the guard, moved on by the fake sleep."""

    def __init__(self) -> None:
        self.now = utcnow()
        self.slept: list[float] = []

    def __call__(self) -> dt.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(linkedin_guard, "utcnow", fake)
    monkeypatch.setattr(linkedin_guard.time, "sleep", fake.sleep)
    return fake


def test_a_short_cooldown_is_waited_out(cfg, clock) -> None:
    guard = CrawlGuard(cfg)
    guard.record_429(retry_after=60.0)

    assert guard.ready() is True
    assert clock.slept == [60.5]


def test_a_long_cooldown_is_not(cfg, clock) -> None:
    guard = CrawlGuard(cfg)
    guard.record_403()

    assert guard.ready() is False
    assert clock.slept == []


def test_three_429s_in_a_row_still_end_the_run(cfg, clock) -> None:
    guard = CrawlGuard(cfg)
    for _ in range(linkedin_guard.BREAKER_AFTER):
        guard.record_429(retry_after=1.0)

    assert guard.ready() is False


def test_a_search_page_refused_once_is_asked_for_again(cfg, clock) -> None:
    from jobhunt.sources.linkedin import LinkedInAdapter
    from tests.test_linkedin_adapter import _page_html, _ref

    responses = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, text=_page_html(["a1"])),
    ]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "seeMoreJobPostings" in str(request.url):
            return responses.pop(0)
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)

    assert len(raw["cards"]) == 1 and "a1" in raw["details"]
    assert adapter.was_truncated() is False
    assert 30.5 in clock.slept


def test_a_detail_page_refused_once_is_asked_for_again(cfg, clock) -> None:
    from jobhunt.sources.linkedin import LinkedInAdapter
    from tests.test_linkedin_adapter import _page_html, _ref

    details = [httpx.Response(429, headers={"Retry-After": "10"}), httpx.Response(200, text="<p>hi</p>")]

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=_page_html(["a1"]))
        return details.pop(0)

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)

    assert "a1" in raw["details"]
    assert adapter.was_truncated() is False
