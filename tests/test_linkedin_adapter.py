"""LinkedIn guest-search adapter.

Fixtures are hand-written to match the guest markup's shape (verified against
the class names and structure documented in the integration plan), not
captured live: the guest endpoint refuses an unauthenticated fetch outside a
browser context, so there is no live response to trim the way the other
adapters' fixtures were.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest

from jobhunt.preferences import Preferences
from jobhunt.sources.base import BoardRef
from jobhunt.sources.linkedin import LinkedInAdapter

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _envelope() -> dict:
    return {
        "cards": [(FIXTURES / "linkedin_search.html").read_text(encoding="utf-8")],
        "details": {"3901234567": (FIXTURES / "linkedin_detail.html").read_text(encoding="utf-8")},
        "posters": {},
    }


def _ref() -> BoardRef:
    return BoardRef(provider="linkedin", token="Backend Engineer|Germany", market="global_remote")


def test_a_card_becomes_a_posting() -> None:
    postings = list(LinkedInAdapter().normalize(_envelope(), _ref()))
    first = next(p for p in postings if p.external_id == "3901234567")
    assert first.title == "Backend Engineer"
    assert first.company_name == "Acme"
    assert first.source == "linkedin"
    assert first.source_url == "https://www.linkedin.com/jobs/view/3901234567"


def test_the_description_comes_from_the_detail_document() -> None:
    posting = next(
        p for p in LinkedInAdapter().normalize(_envelope(), _ref()) if p.external_id == "3901234567"
    )
    assert "You will write Python" in (posting.description_text or "")
    assert posting.jd_completeness == "full"


def test_a_card_without_a_detail_document_is_still_a_posting() -> None:
    envelope = _envelope() | {"details": {}}
    posting = next(
        p for p in LinkedInAdapter().normalize(envelope, _ref()) if p.external_id == "3901234567"
    )
    assert posting.jd_completeness == "none"


def test_the_hiring_team_is_carried_through() -> None:
    postings = list(LinkedInAdapter().normalize(_envelope(), _ref()))
    posting = next(p for p in postings if p.external_id == "3901234567")
    assert posting.poster_name == "Jane Doe"
    assert posting.poster_profile_url == "https://www.linkedin.com/in/jane-doe-1234"


def test_malformed_html_yields_nothing_rather_than_raising() -> None:
    assert list(LinkedInAdapter().normalize({"cards": ["<nonsense"], "details": {}}, _ref())) == []


def test_board_refs_are_one_per_title_and_location() -> None:
    prefs = Preferences(titles=["Backend Engineer", "Platform Engineer"], locations=["Germany"])
    refs = LinkedInAdapter().board_refs(prefs)
    assert len(refs) == 2
    assert {r.token for r in refs} == {"Backend Engineer|Germany", "Platform Engineer|Germany"}


def test_a_search_with_no_location_still_produces_one_ref() -> None:
    refs = LinkedInAdapter().board_refs(Preferences(titles=["Backend Engineer"]))
    assert [r.token for r in refs] == ["Backend Engineer|"]


def test_fetch_stops_when_the_guard_refuses(cfg) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, text="<li></li>")

    adapter = LinkedInAdapter(config=cfg)
    adapter.guard.tripped = True
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert calls == []


def test_a_429_is_recorded_and_does_not_raise(cfg) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "42"})

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert adapter.guard.allow() is False


def _card_html(job_id: str) -> str:
    return (
        f'<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
        '<h3 class="base-search-card__title">Backend Engineer</h3>'
        '<h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/acme">Acme'
        "</a></h4>"
        '<span class="job-search-card__location">Remote</span>'
        f'<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{job_id}/"></a>'
        "</div></li>"
    )


def _page_html(ids: list[str]) -> str:
    return "<ul>" + "".join(_card_html(job_id) for job_id in ids) + "</ul>"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The guard's real delay is a few seconds; tests only need it invoked."""
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr("jobhunt.sources.linkedin.time.sleep", fake_sleep)
    return calls


def test_pagination_stops_at_max_pages(cfg) -> None:
    """A source that never runs out of full pages must still be bounded by MAX_PAGES,
    not by its own claim to have more."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, text=_page_html([f"id{i}" for i in range(10)]))

    adapter = LinkedInAdapter(config=cfg, known_ids={f"id{i}" for i in range(10)})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert len(calls) == LinkedInAdapter.MAX_PAGES


def test_a_short_page_ends_pagination(cfg) -> None:
    pages = [_page_html([f"p1-{i}" for i in range(10)]), _page_html(["p2-0", "p2-1"])]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, text=pages[len(calls) - 1])

    known = {f"p1-{i}" for i in range(10)} | {"p2-0", "p2-1"}
    adapter = LinkedInAdapter(config=cfg, known_ids=known)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert len(calls) == 2


def test_known_ids_are_never_fetched_for_detail(cfg) -> None:
    search_calls = []
    detail_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            search_calls.append(request.url)
            return httpx.Response(200, text=_page_html(["known1", "fresh1"]))
        detail_calls.append(request.url)
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg, known_ids={"known1"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)
    assert len(detail_calls) == 1
    assert "fresh1" in str(detail_calls[0])
    assert "known1" not in raw["details"]
    assert "fresh1" in raw["details"]


def test_a_403_trips_the_breaker_and_stops(cfg) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(403)

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert len(calls) == 1
    assert adapter.guard.tripped is True
    assert adapter.guard.allow() is False


def test_a_malformed_retry_after_does_not_raise(cfg) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "nan"})

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)  # must not raise
    assert adapter.guard.allow() is False


def test_the_detail_loop_paces_itself(cfg, _no_real_sleeping: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=_page_html(["a1", "a2"]))
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)
    assert len(raw["details"]) == 2
    # One search page short of a full page never sleeps between pages, so every
    # recorded sleep here belongs to the detail loop - one per detail fetch,
    # including the one that follows the search page.
    assert len(_no_real_sleeping) == 2

