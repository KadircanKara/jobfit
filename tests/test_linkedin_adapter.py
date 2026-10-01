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

from jobhunt.extract import quality
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
    assert first.apply_url == (
        "https://de.linkedin.com/jobs/view/backend-engineer-at-acme-3901234567"
    )


def test_the_description_comes_from_the_detail_document() -> None:
    posting = next(
        p for p in LinkedInAdapter().normalize(_envelope(), _ref()) if p.external_id == "3901234567"
    )
    assert "You will write Python" in (posting.description_text or "")
    assert posting.jd_completeness == "full"


def test_show_more_show_less_buttons_do_not_survive_into_the_description() -> None:
    """`linkedin_detail.html` carries LinkedIn's real button markup: two
    `show-more-less-html__button` elements (`public_jobs_show-more-html-btn` and
    `public_jobs_show-less-html-btn`) with "Show more" / "Show less" as their
    literal text. Left in place, `TRUNCATION_MARKERS` reads a complete JD as
    clipped, and a run-on paragraph with no newlines reads a six-word EEO
    trigger as 100 percent boilerplate. Both must be gone, and the fix must
    have done it by removing the button elements, not by string-replacing the
    words - a JD that genuinely ends with "read more" would fail this test
    for the wrong reason if it did not.
    """
    posting = next(
        p for p in LinkedInAdapter().normalize(_envelope(), _ref()) if p.external_id == "3901234567"
    )
    text = posting.description_text or ""
    assert "show more" not in text.lower()
    assert "show less" not in text.lower()
    assert "\n" in text, "paragraph breaks must survive as newlines"
    result = quality.assess(text)
    assert result.passed, result.reasons


def test_a_card_without_a_detail_document_is_still_a_posting() -> None:
    envelope = _envelope() | {"details": {}}
    posting = next(
        p for p in LinkedInAdapter().normalize(envelope, _ref()) if p.external_id == "3901234567"
    )
    assert posting.jd_completeness == "none"


def test_the_hiring_team_is_carried_through() -> None:
    """`linkedin_detail.html` carries the real guest markup: a
    `message-the-recruiter` block linking to the poster's regional subdomain
    with a `trk` tracking query. Both must be gone from what is stored, or
    the profile URL dedupes and resolves through Unipile as a different
    person from the same profile reached via `www.linkedin.com`.
    """
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


def test_a_card_whose_title_stage_1_drops_is_not_fetched_for_detail(cfg, monkeypatch) -> None:
    """A detail page costs one of roughly ten requests a minute; a title the
    user is not looking for is not worth one. The card itself is still kept."""
    from jobhunt import board_scope

    monkeypatch.setattr(
        board_scope.deterministic, "load_filters",
        lambda config: {"global": {"require_titles_regex": ["(?i)backend"]}},
    )
    other = _card_html("other").replace("Backend Engineer", "Bakery Lead")
    page = "<ul>" + _card_html("wanted") + other + "</ul>"
    detail_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=page)
        detail_calls.append(str(request.url))
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)

    assert len(detail_calls) == 1 and "wanted" in detail_calls[0]
    assert {posting.external_id for posting in adapter.normalize(raw, _ref())} == {"wanted", "other"}
    assert not adapter.was_truncated()


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
    """Asserting only a sleep count would pass a `sleep(0)` too. Stubbing
    `guard.delay()` to a sentinel and checking the exact slept values proves
    the pacing actually came from the guard, not merely that *a* sleep ran."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=_page_html(["a1", "a2"]))
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg)
    adapter.guard.delay = lambda: 7.25  # sentinel, unmistakable if it's ever slept
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter.fetch(_ref(), client)
    assert len(raw["details"]) == 2
    # One search page short of a full page never sleeps between pages, so every
    # recorded sleep here belongs to the detail loop - one per detail fetch,
    # including the one that follows the search page - and each is the exact
    # value the guard handed back.
    assert _no_real_sleeping == [7.25, 7.25]


def test_the_guard_refusing_mid_loop_stops_the_detail_pass_immediately(cfg) -> None:
    """`continue` and `break` both skip the request for the id that first sees a
    refusal, so counting *requests* can't tell them apart - both send none for
    it. What differs is whether every id *after* that one still pays for a
    guard check (`continue` walks them all; `break` stops at the first).
    Counting only the guard checks made while the guard is already tripped
    isolates that, regardless of how many calls the search-pagination loop
    made before the guard ever tripped."""
    ids = [f"d{i}" for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=_page_html(ids))
        detail_calls.append(request.url)
        if len(detail_calls) == 2:
            adapter.guard.tripped = True
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    detail_calls: list = []
    adapter = LinkedInAdapter(config=cfg)
    calls_while_tripped: list[str | None] = []
    real_refusal = adapter.guard.refusal

    # `allow` delegates to `refusal`, so spying here catches both spellings.
    def spy_refusal(*args: object, **kwargs: object) -> str | None:
        was_already_tripped = adapter.guard.tripped
        result = real_refusal(*args, **kwargs)
        if was_already_tripped:
            calls_while_tripped.append(result)
        return result

    adapter.guard.refusal = spy_refusal
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    # d0 and d1 are requested and the second trips the guard. d2 sees the trip
    # and must stop the pass right there - d3 and d4 must never even be asked
    # about, which is exactly the one guard check this asserts.
    assert len(detail_calls) == 2
    assert calls_while_tripped == ["the circuit breaker tripped for this run"]


def _card_html_missing_urn() -> str:
    """A card LinkedIn served without (or with a malformed) `data-entity-urn`.

    Still a real card for pagination purposes - it takes up a slot on the
    page - it just yields no id.
    """
    return (
        "<li><div class=\"base-card\">"
        '<h3 class="base-search-card__title">Backend Engineer</h3>'
        '<h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/acme">Acme'
        "</a></h4>"
        '<span class="job-search-card__location">Remote</span>'
        "</div></li>"
    )


def test_a_card_missing_its_urn_does_not_look_like_a_short_page(cfg) -> None:
    """Sizing a page by id count rather than card count reads a full page with
    one un-parseable card as a short page and stops pagination early."""
    page1_ids = [f"p1-{i}" for i in range(9)]
    page1 = "<ul>" + "".join(_card_html(job_id) for job_id in page1_ids) + _card_html_missing_urn() + "</ul>"
    page2 = _page_html(["p2-0", "p2-1"])
    pages = [page1, page2]
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, text=pages[len(calls) - 1])

    known = set(page1_ids) | {"p2-0", "p2-1"}
    adapter = LinkedInAdapter(config=cfg, known_ids=known)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert len(calls) == 2



def test_a_refused_fetch_says_so_afterwards(cfg) -> None:
    """A refused fetch that does not admit it was refused is read downstream as
    "this search found nothing", which retires jobs the ref still has."""
    adapter = LinkedInAdapter(config=cfg)
    adapter.guard.tripped = True
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        adapter.fetch(_ref(), client)
    assert adapter.was_truncated() is True


def test_a_complete_pass_is_not_marked_truncated(cfg) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=_page_html(["only1"]))
        return httpx.Response(200, text="<div class='description__text'>hi</div>")

    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert adapter.was_truncated() is False


def test_a_403_truncates_and_is_logged(cfg, caplog) -> None:
    adapter = LinkedInAdapter(config=cfg)
    with caplog.at_level("WARNING", logger="jobhunt.sources.linkedin"):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            adapter.fetch(_ref(), client)
    assert adapter.was_truncated() is True
    assert "403" in caplog.text


def test_a_403_persists_a_cooldown_so_the_next_run_does_not_hammer(cfg) -> None:
    """The breaker is per-run. Without a persisted cooldown the next run starts
    hopeful and goes straight back at a source that just refused us outright."""
    adapter = LinkedInAdapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
        adapter.fetch(_ref(), client)
    assert adapter.guard.cooling_until() is not None
    assert LinkedInAdapter(config=cfg).guard.allow() is False


def test_a_short_page_that_exhausts_the_budget_is_not_truncated(cfg, monkeypatch) -> None:
    """A short page ends the search naturally. Checking the guard before that
    natural-completion condition marks the pass truncated whenever the budget
    happens to run out on the very request that produced the short page - that
    is a pass that finished, not one that was refused."""
    from jobhunt.sources import linkedin_guard

    DAILY_BUDGET = 400
    monkeypatch.setattr(linkedin_guard, "DAILY_BUDGET", DAILY_BUDGET)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page_html(["only1"]))

    adapter = LinkedInAdapter(config=cfg, known_ids={"only1"})
    adapter.guard.spend(DAILY_BUDGET - 1)  # this page's own request exhausts it
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert adapter.was_truncated() is False


def test_a_detail_pass_with_only_known_ids_is_not_truncated(cfg) -> None:
    """Every id from the search is already in the corpus, so the detail loop
    has nothing left to fetch. Checking the guard before the known-ids `continue`
    marks it truncated anyway whenever the guard happens to be exhausted by
    then, even though nothing was actually skipped."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "seeMoreJobPostings" in str(request.url):
            # Exhausted the instant the search page lands, before the detail
            # loop even starts - proves the detail loop's own ordering, not
            # the search loop's.
            adapter.guard.tripped = True
            return httpx.Response(200, text=_page_html(["only1"]))
        raise AssertionError("a known id must never be fetched for detail")

    adapter = LinkedInAdapter(config=cfg, known_ids={"only1"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter.fetch(_ref(), client)
    assert adapter.was_truncated() is False


def test_an_exhausted_guard_stops_the_fetch_loop_pacing(cfg) -> None:
    adapter = LinkedInAdapter(config=cfg)
    assert adapter.still_fetching() is True
    adapter.guard.tripped = True
    assert adapter.still_fetching() is False


def test_a_relative_hirer_link_becomes_an_absolute_profile_url(cfg) -> None:
    """LinkedIn's hirer card commonly emits `/in/slug`. Stored verbatim it has no
    `linkedin.com/in/` in it, so the Unipile provider refuses to send to it."""
    detail = (
        "<div class='description__text'>hi</div>"
        "<div class='hirer-card__hirer-information'><a href='/in/jane-doe/'>Jane Doe</a></div>"
    )
    postings = list(
        LinkedInAdapter().normalize(
            {"cards": [_page_html(["j1"])], "details": {"j1": detail}}, _ref()
        )
    )
    assert postings[0].poster_profile_url == "https://www.linkedin.com/in/jane-doe"
