"""The shortlist shows only postings that are still open. No network: every
page comes from an httpx.MockTransport."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from jobhunt.db.models import Job, utcnow
from jobhunt.db.session import session_scope
from jobhunt.pipeline import jd_fetch, liveness
from jobhunt.render import review
from jobhunt.web import engine
from tests.test_review import make_scored

OPEN_PAGE = "<html><body><h1>AI Engineer</h1><p>Apply now.</p></body></html>"
CLOSED_PAGE = (
    "<html><body><h1>AI Engineer</h1>"
    "<figure class='closed-job'><figcaption>No longer accepting applications</figcaption></figure>"
    "</body></html>"
)


class FakeGuard:
    def __init__(self, allow: bool = True) -> None:
        self.allowed = allow
        self.spent = 0
        self.events: list[str] = []

    def allow(self) -> bool:
        return self.allowed

    def delay(self) -> float:
        return 0.0

    def spend(self, n: int = 1) -> None:
        self.spent += n

    def record_ok(self) -> None:
        self.events.append("ok")

    def record_429(self, retry_after) -> None:
        self.events.append("429")
        self.allowed = False

    def record_403(self) -> None:
        self.events.append("403")
        self.allowed = False


@pytest.fixture(autouse=True)
def _public_hosts(monkeypatch):
    # The SSRF check resolves hosts; the pages here never leave the process.
    monkeypatch.setattr(jd_fetch, "check", lambda url: None)


def client_for(pages: dict[str, httpx.Response], seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        for prefix, response in pages.items():
            if url.startswith(prefix):
                return response
        return httpx.Response(500)

    return httpx.Client(transport=httpx.MockTransport(handler))


def html(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"content-type": "text/html"})


def linkedin_job(cfg, external_id="4460243692", **kwargs) -> int:
    return make_scored(
        cfg, source="linkedin", external_id=external_id,
        apply_url=f"https://www.linkedin.com/jobs/view/{external_id}", **kwargs,
    )


def job(cfg, job_id: int) -> Job:
    with session_scope(cfg.db_path) as session:
        row = session.get(Job, job_id)
        session.expunge(row)
        return row


# --- reading a page ------------------------------------------------------------


def test_a_closed_notice_on_the_page_reads_as_closed() -> None:
    assert liveness.says_closed(CLOSED_PAGE)
    assert not liveness.says_closed(OPEN_PAGE)


def test_a_notice_only_inside_a_script_is_not_the_page_saying_it() -> None:
    page = "<html><script>const t={gone:'This job is no longer available'}</script><p>Apply.</p></html>"
    assert not liveness.says_closed(page)


# --- LinkedIn ------------------------------------------------------------------


def test_a_linkedin_posting_no_longer_accepting_applications_is_retired(cfg) -> None:
    job_id = linkedin_job(cfg)
    client = client_for({"https://www.linkedin.com/jobs-guest/": html(CLOSED_PAGE)})

    result = liveness.verify(cfg, [job_id], client=client, guard=FakeGuard())

    assert result.closed == {job_id}
    assert job(cfg, job_id).is_active is False
    assert review.shortlist(cfg) == []


def test_a_linkedin_posting_that_is_gone_is_retired(cfg) -> None:
    job_id = linkedin_job(cfg)
    client = client_for({"https://www.linkedin.com/jobs-guest/": html("", 404)})

    assert liveness.verify(cfg, [job_id], client=client, guard=FakeGuard()).closed == {job_id}


def test_an_open_posting_is_stamped_and_not_read_again_the_same_day(cfg) -> None:
    job_id = linkedin_job(cfg)
    seen: list[str] = []
    client = client_for({"https://www.linkedin.com/jobs-guest/": html(OPEN_PAGE)}, seen)

    first = liveness.verify(cfg, [job_id], client=client, guard=FakeGuard())
    second = liveness.verify(cfg, [job_id], client=client, guard=FakeGuard())

    assert first.confirmed_open == 1 and not first.hidden
    assert not second.hidden
    assert len(seen) == 1
    assert job(cfg, job_id).open_checked_at is not None


def test_a_stamp_older_than_a_day_is_read_again(cfg) -> None:
    job_id = linkedin_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).open_checked_at = utcnow() - dt.timedelta(hours=25)
    seen: list[str] = []
    client = client_for({"https://www.linkedin.com/jobs-guest/": html(OPEN_PAGE)}, seen)

    liveness.verify(cfg, [job_id], client=client, guard=FakeGuard())

    assert len(seen) == 1


def test_a_linkedin_job_the_guard_will_not_check_is_held_back_not_retired(cfg) -> None:
    job_id = linkedin_job(cfg)
    seen: list[str] = []
    client = client_for({}, seen)

    result = liveness.verify(cfg, [job_id], client=client, guard=FakeGuard(allow=False))

    assert result.unchecked == {job_id} and result.hidden == {job_id}
    assert seen == []
    assert job(cfg, job_id).is_active is True


def test_a_429_stops_every_later_linkedin_check(cfg) -> None:
    first = linkedin_job(cfg, external_id="1")
    second = linkedin_job(cfg, external_id="2")
    seen: list[str] = []
    guard = FakeGuard()
    client = client_for({"https://www.linkedin.com/jobs-guest/": httpx.Response(429)}, seen)

    result = liveness.verify(cfg, [first, second], client=client, guard=guard)

    assert result.unchecked == {first, second}
    assert len(seen) == 1
    assert guard.events == ["429"]


# --- other boards ----------------------------------------------------------------


def test_a_board_posting_that_404s_is_retired(cfg) -> None:
    job_id = make_scored(cfg)
    client = client_for({"https://jobs.ashbyhq.com/": html("", 404)})

    assert liveness.verify(cfg, [job_id], client=client).closed == {job_id}


def test_a_redirect_to_greenhouse_error_page_is_retired(cfg) -> None:
    job_id = make_scored(
        cfg, source="greenhouse", external_id="7001",
        apply_url="https://boards.greenhouse.io/acme/jobs/7001",
    )
    client = client_for({
        "https://boards.greenhouse.io/acme/jobs/7001": httpx.Response(
            302, headers={"location": "https://boards.greenhouse.io/acme?error=true"}
        ),
        "https://boards.greenhouse.io/acme?error=true": html(OPEN_PAGE),
    })

    assert liveness.verify(cfg, [job_id], client=client).closed == {job_id}


def test_a_redirect_that_drops_the_job_id_is_retired(cfg) -> None:
    job_id = make_scored(
        cfg, source="lever", external_id="abc-123",
        apply_url="https://jobs.lever.co/acme/abc-123",
    )
    client = client_for({
        "https://jobs.lever.co/acme/abc-123": httpx.Response(
            301, headers={"location": "https://jobs.lever.co/acme"}
        ),
        "https://jobs.lever.co/acme": html(OPEN_PAGE),
    })

    assert liveness.verify(cfg, [job_id], client=client).closed == {job_id}


def test_a_board_that_errors_keeps_its_job_on_the_listing_s_word(cfg) -> None:
    job_id = make_scored(cfg)
    client = client_for({"https://jobs.ashbyhq.com/": httpx.Response(503)})

    result = liveness.verify(cfg, [job_id], client=client)

    assert not result.hidden
    row = job(cfg, job_id)
    assert row.is_active is True and row.open_checked_at is None


# --- the shortlist ---------------------------------------------------------------


def test_a_job_no_listing_has_carried_for_two_weeks_is_not_shortlisted(cfg) -> None:
    job_id = linkedin_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).last_seen_at = utcnow() - dt.timedelta(days=review.SEEN_WITHIN_DAYS + 1)

    assert review.shortlist(cfg) == []


def test_a_closed_job_gives_its_place_to_the_next_one(cfg) -> None:
    closed = make_scored(cfg, score_value=0.9, external_id="a")
    kept = make_scored(cfg, score_value=0.8, external_id="b")
    runner_up = make_scored(cfg, score_value=0.75, external_id="c")
    calls: list[list[int]] = []

    def verify(config, ids):
        calls.append(list(ids))
        result = liveness.Result()
        if closed in ids:
            with session_scope(config.db_path) as session:
                session.get(Job, closed).is_active = False
            result.closed.add(closed)
        return result

    cards, _ = engine.open_cards(cfg, limit=2, near=0, verify=verify)

    assert [card.job_id for card in cards] == [kept, runner_up]
    assert calls == [[closed, kept], [runner_up]]


def test_an_unchecked_job_is_left_off_this_shortlist_only(cfg) -> None:
    held = make_scored(cfg, score_value=0.9, external_id="a")
    shown = make_scored(cfg, score_value=0.8, external_id="b")

    def verify(config, ids):
        result = liveness.Result()
        result.unchecked = {held} & set(ids)
        return result

    cards, _ = engine.open_cards(cfg, limit=2, near=0, verify=verify)

    assert [card.job_id for card in cards] == [shown]
    assert job(cfg, held).is_active is True
