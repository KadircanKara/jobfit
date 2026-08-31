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
