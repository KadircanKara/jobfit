"""Workable's cross-company job search. No network: the HTTP client is a
MockTransport, and throttle's waits are recorded instead of slept.

The job shape is trimmed from a live `jobs.workable.com/api/v1/jobs` response
(2026-09-30); only the description text is shortened.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from jobhunt import preferences as prefs_module
from jobhunt import sync
from jobhunt.db.models import Board, Job
from jobhunt.db.session import session_scope
from jobhunt.sources import throttle
from jobhunt.sources.base import BoardRef, RateLimit
from jobhunt.sources.workable_search import SEARCH_URL, WorkableSearchAdapter
from jobhunt.web.app import board_sources

BODY = (
    "<p><strong>The role is about:</strong></p><p>We are expanding our engineering capabilities "
    "and looking for a Senior Backend Engineer to join our growing team. You will design and build "
    "scalable, high-performance backend systems in Python, own services end to end, and work with "
    "product and data teams on payments infrastructure used by thousands of merchants.</p>"
    "<p>You will review code, mentor engineers, and improve observability, reliability and the "
    "speed at which the team can ship safely to production every day.</p>"
)

JOB = {
    "department": "IT",
    "id": "eb0b7e5f-521e-476f-bffc-15b7d4701504",
    "title": "Senior Backend Python Developer",
    "state": "published",
    "description": BODY,
    "employmentType": "Full-time",
    "benefitsSection": "",
    "requirementsSection": "<ul><li>5+ years of Python</li></ul>",
    "url": "https://jobs.workable.com/view/v2pUbGewzPa8EBVZ9KNkrf/remote-senior-backend-python-developer",
    "language": "en",
    "locations": ["TELECOMMUTE", "Germany"],
    "location": {"city": "", "subregion": None, "countryName": "Germany"},
    "created": "2026-06-23T14:58:09.427Z",
    "updated": "2026-06-23T14:58:09.427Z",
    "company": {
        "id": "da0005c5-0da3-4fac-94cf-90f2aa0c4760",
        "title": "payabl.",
        "website": "http://payabl.com",
        "url": "https://jobs.workable.com/company/sVkt9hjnPaomoq9k7Rs6ZA/jobs-at-payabl.",
    },
    "isFeatured": False,
    "workplace": "remote",
}


def job(**changes) -> dict:
    out = copy.deepcopy(JOB)
    out.update(changes)
    return out


def page(jobs: list[dict], next_token: str | None = None, total: int | None = None) -> dict:
    out = {"title": "Workable", "totalSize": total or len(jobs), "jobs": jobs, "autoAppliedFilters": {}}
    if next_token:
        out["nextPageToken"] = next_token
    return out


def ref(token: str = "Backend Engineer|Germany", workplace: list[str] | None = None) -> BoardRef:
    return BoardRef(
        provider="workable_search", token=token, market="global_remote",
        extra={"workplace": workplace or []},
    )


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch) -> None:
    monkeypatch.setattr(WorkableSearchAdapter, "rate_limit", RateLimit(0.0))


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# --- parsing -------------------------------------------------------------------


def test_a_search_result_becomes_a_posting() -> None:
    [posting] = WorkableSearchAdapter().normalize({"pages": [page([JOB])]}, ref())

    assert posting.source == "workable_search"
    assert posting.external_id == JOB["id"]
    assert posting.title == "Senior Backend Python Developer"
    assert posting.company_name == "payabl."
    assert posting.country == "DE"
    assert posting.city is None
    assert posting.location_raw == "Germany"
    assert posting.remote_type == "remote"
    assert posting.employment_type == "full_time"
    assert posting.apply_url == JOB["url"]
    assert posting.source_url == JOB["url"]
    assert posting.departments == ["IT"]
    assert posting.posted_at is not None and posting.posted_at.year == 2026


def test_the_description_carries_the_requirements_and_counts_as_full() -> None:
    [posting] = WorkableSearchAdapter().normalize({"pages": [page([JOB])]}, ref())

    assert "backend systems in Python" in posting.description_text
    assert "5+ years of Python" in posting.description_text
    assert posting.jd_completeness == "full"
    assert posting.jd_source == "api"


def test_workplace_and_city_map_to_our_vocabulary() -> None:
    onsite = job(
        id="x1", workplace="on_site", locations=["Munich, Bavaria, Germany"],
        location={"city": "Munich", "subregion": "Bavaria", "countryName": "Germany"},
    )
    hybrid = job(id="x2", workplace="hybrid")
    raw = {"pages": [page([onsite, hybrid])]}
    postings = {p.external_id: p for p in WorkableSearchAdapter().normalize(raw, ref())}

    assert postings["x1"].remote_type == "onsite"
    assert postings["x1"].city == "Munich"
    assert postings["x1"].location_raw == "Munich, Bavaria, Germany"
    assert postings["x2"].remote_type == "hybrid"


def test_a_linkout_is_the_apply_url() -> None:
    [posting] = WorkableSearchAdapter().normalize(
        {"pages": [page([job(linkoutUrl="https://careers.example.com/apply/1")])]}, ref()
    )
    assert posting.apply_url == "https://careers.example.com/apply/1"
    assert posting.source_url == JOB["url"]


def test_a_job_seen_on_two_pages_is_yielded_once_and_junk_is_skipped() -> None:
    raw = {"pages": [page([JOB, {"id": "no-title"}, "junk"]), page([JOB, job(id="other", title="Other")])]}
    ids = [p.external_id for p in WorkableSearchAdapter().normalize(raw, ref())]
    assert ids == [JOB["id"], "other"]


# --- the query -----------------------------------------------------------------


def test_refs_are_one_search_per_title_and_location() -> None:
    prefs = prefs_module.Preferences(
        titles=["Backend Engineer", "Data Engineer"],
        locations=["Germany", "Remote"],
        work_model=["remote", "hybrid"],
    )
    refs = WorkableSearchAdapter().board_refs(prefs)

    assert [r.token for r in refs] == [
        "Backend Engineer|Germany", "Backend Engineer|Remote",
        "Data Engineer|Germany", "Data Engineer|Remote",
    ]
    assert refs[0].extra == {"workplace": ["hybrid", "remote"]}


def test_params_repeat_workplace_and_leave_anywhere_out() -> None:
    adapter = WorkableSearchAdapter()

    assert adapter.params(ref("Backend Engineer|Germany", ["hybrid", "remote"]), None) == [
        ("query", "Backend Engineer"), ("location", "Germany"),
        ("workplace", "hybrid"), ("workplace", "remote"),
    ]
    # "Remote" names no place, and all three workplaces filter nothing.
    assert adapter.params(ref("Backend Engineer|Remote", ["hybrid", "on_site", "remote"]), "tok") == [
        ("query", "Backend Engineer"), ("pageToken", "tok"),
    ]


# --- pagination ------------------------------------------------------------------


def test_pages_are_followed_until_there_is_no_next_token() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(SEARCH_URL)
        params = query(request)
        seen.append(params)
        if "pageToken" not in params:
            return httpx.Response(200, json=page([JOB], next_token="p2", total=2))
        return httpx.Response(200, json=page([job(id="second")]))

    raw = WorkableSearchAdapter().fetch(ref(), client_for(handler))

    assert len(seen) == 2
    assert seen[1]["pageToken"] == ["p2"]
    assert [p.external_id for p in WorkableSearchAdapter().normalize(raw, ref())] == [JOB["id"], "second"]


def test_pagination_stops_at_the_page_cap() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        n = len(calls)
        return httpx.Response(200, json=page([job(id=f"j{n}")], next_token=f"p{n}", total=9999))

    raw = WorkableSearchAdapter().fetch(ref(), client_for(handler))

    assert len(calls) == WorkableSearchAdapter.MAX_PAGES
    assert len(raw["pages"]) == WorkableSearchAdapter.MAX_PAGES


def test_an_empty_page_ends_the_search_even_with_a_token() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=page([], next_token="again"))

    WorkableSearchAdapter().fetch(ref(), client_for(handler))
    assert len(calls) == 1


# --- 429s go through throttle ---------------------------------------------------------


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(throttle, "pause", lambda seconds, should_stop=None: calls.append(seconds) or True)
    return calls


def mock_client(monkeypatch, handler) -> None:
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sync.httpx, "Client", fake_client)


def save_prefs(cfg, **fields) -> None:
    prefs, _ = prefs_module.load(cfg)
    prefs.titles = fields.get("titles", ["Backend Engineer"])
    prefs.locations = fields.get("locations", ["Germany"])
    prefs_module.save(cfg, prefs)


def test_a_429_is_waited_out_and_the_search_resumes_at_the_refused_page(cfg, monkeypatch, slept) -> None:
    save_prefs(cfg)
    asked: list[str | None] = []
    refused = {"left": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        token = (query(request).get("pageToken") or [None])[0]
        asked.append(token)
        if token is None:
            return httpx.Response(200, json=page([JOB], next_token="p2", total=2))
        if refused["left"]:
            refused["left"] -= 1
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json=page([job(id="second")]))

    mock_client(monkeypatch, handler)
    result = sync.sync_source(cfg, "workable_search")

    # Page one is not asked for again after the wait.
    assert asked == [None, "p2", "p2"]
    assert slept == [5.0]
    assert (result.status, result.new, result.errors, result.throttled) == ("ok", 2, 0, 0)
    [event] = throttle.history(cfg)
    assert (event["source"], event["board"], event["status"]) == (
        "workable_search", "Backend Engineer|Germany", 429,
    )


def test_a_search_that_keeps_refusing_is_left_for_the_next_run_not_failed(cfg, monkeypatch, slept) -> None:
    save_prefs(cfg)
    mock_client(monkeypatch, lambda request: httpx.Response(429))

    result = sync.sync_source(cfg, "workable_search")

    assert result.throttled == 1
    assert result.errors == 0
    assert result.status == "degraded"
    assert sum(slept) <= throttle.MAX_WAIT_PER_SOURCE


# --- wiring --------------------------------------------------------------------------


def test_the_adapter_is_built_with_refs_from_preferences(cfg) -> None:
    save_prefs(cfg, titles=["Backend Engineer"], locations=["Germany", "Netherlands"])

    adapter = sync.build_adapter(cfg, "workable_search")

    tokens = [r.token for r in adapter.discover()]
    assert tokens == ["Backend Engineer|Germany", "Backend Engineer|Netherlands"]


def test_it_is_a_search_not_a_board_source() -> None:
    assert "workable_search" not in board_sources()
    assert "workable" in board_sources()


def test_a_board_pass_never_starts_a_search(cfg, monkeypatch) -> None:
    save_prefs(cfg)
    mock_client(monkeypatch, lambda request: pytest.fail("a candidate pass must not search"))

    result = sync.sync_source(cfg, "workable_search", only_status="candidate")
    assert result.boards == 0


# --- dedupe ----------------------------------------------------------------------------


def place_raw(cfg, source: str, token: str, run_key: str, payload: dict) -> None:
    directory = sync.raw_dir(cfg, source, run_key)
    directory.mkdir(parents=True, exist_ok=True)
    envelope = {
        "run_key": run_key, "source": source, "provider": source, "token": token,
        "market": "global_remote", "board_id": None, "fetched_at": "2026-09-30T00:00:00",
        "payload": payload,
    }
    safe = token.replace("|", "_").replace(" ", "_")
    (directory / f"{source}__{safe}.json").write_text(json.dumps(envelope), encoding="utf-8")


def board_payload() -> dict:
    """The same posting as payabl.'s own Workable board serves it: one body
    with the requirements inside, a shortcode, no search id."""
    return {
        "name": "payabl.",
        "jobs": [{
            "title": "Senior Backend Python Developer",
            "shortcode": "A1B2C3D4E5",
            "employment_type": "Full-time",
            "telecommuting": True,
            "department": "IT",
            "url": "https://apply.workable.com/j/A1B2C3D4E5",
            "application_url": "https://apply.workable.com/j/A1B2C3D4E5/apply",
            "published_on": "2026-06-23",
            "country": "Germany",
            "city": "",
            "locations": [{"country": "Germany", "countryCode": "DE", "city": "", "hidden": False}],
            "description": BODY + "<h3>Requirements</h3><ul><li>5+ years of Python</li></ul>",
        }],
    }


def test_a_search_hit_and_its_board_copy_are_one_job(cfg) -> None:
    place_raw(cfg, "workable", "payabl", "R1", board_payload())
    sync.normalize_pass(cfg, "workable", "R1")
    place_raw(cfg, "workable_search", "Backend Engineer|Germany", "R2", {"pages": [page([JOB])]})
    sync.normalize_pass(cfg, "workable_search", "R2")

    with session_scope(cfg.db_path) as session:
        board_job = session.query(Job).filter_by(source="workable").one()
        search_job = session.query(Job).filter_by(source="workable_search").one()
        assert board_job.company_id == search_job.company_id
        # One card: the search copy clusters under the board copy seen first.
        assert search_job.canonical_job_id == board_job.id
        assert board_job.canonical_job_id == board_job.id


def test_the_same_hit_from_two_searches_is_one_row(cfg) -> None:
    place_raw(cfg, "workable_search", "Backend Engineer|Germany", "R1", {"pages": [page([JOB])]})
    place_raw(cfg, "workable_search", "Python Developer|Germany", "R1", {"pages": [page([JOB])]})
    result = sync.normalize_pass(cfg, "workable_search", "R1")

    assert (result.new, result.unchanged) == (1, 1)
    with session_scope(cfg.db_path) as session:
        assert session.query(Job).filter_by(source="workable_search").count() == 1


def test_a_different_job_at_the_same_company_stays_separate(cfg) -> None:
    place_raw(cfg, "workable", "payabl", "R1", board_payload())
    sync.normalize_pass(cfg, "workable", "R1")
    other = job(id="other", title="Product Designer", description="<p>" + "Design things. " * 60 + "</p>")
    place_raw(cfg, "workable_search", "Designer|Germany", "R2", {"pages": [page([other])]})
    sync.normalize_pass(cfg, "workable_search", "R2")

    with session_scope(cfg.db_path) as session:
        search_job = session.query(Job).filter_by(source="workable_search").one()
        assert search_job.canonical_job_id == search_job.id
        # The search makes no company board: it never learns the account token.
        assert session.query(Board).filter_by(provider="workable").count() == 1
