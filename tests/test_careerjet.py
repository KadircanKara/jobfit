"""Careerjet's job search API, Turkish locale. No network: the HTTP client is a
MockTransport.

The job shape is a live tr_TR response (2026-10-04), with the tracker URL and
excerpt shortened. The v4 docs give the same fields plus the salary ones.
"""
from __future__ import annotations

import base64
import copy
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from jobhunt import preferences as prefs_module
from jobhunt.sources.base import BoardRef, RateLimit
from jobhunt.sources.careerjet import (
    KEY_ENV,
    SEARCH_URL,
    USER_IP_ENV,
    CareerjetAdapter,
    MissingApiKey,
    search_location,
)

JOB = {
    "locations": "Yenimahalle, Ankara",
    "site": "",
    "date": "Fri, 02 Oct 2026 04:41:27 GMT",
    "url": "https://jobviewtrack.com/v2/u-FwJGrfrKqu8SRPG6oqbhEYZql2XHgIhsUC9",
    "title": "YAZILIM GELİŞTİRME PERSONELİ",
    "description": "Tercihen Bilgisayar Mühendisliği mezunu, <b>Yazılım</b> geliştirme konusunda deneyimli...",
    "company": "TURKSAT",
    "salary": "",
}


def job(**changes) -> dict:
    out = copy.deepcopy(JOB)
    out.update(changes)
    return out


def page(jobs: list[dict], pages: int = 1) -> dict:
    return {"type": "JOBS", "hits": len(jobs), "pages": pages, "message": "", "jobs": jobs}


def ref(token: str = "Yazılım Geliştirici|Ankara", **extra) -> BoardRef:
    return BoardRef(provider="careerjet", token=token, market="tr_local", extra=extra)


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch) -> None:
    monkeypatch.setattr(CareerjetAdapter, "rate_limit", RateLimit(0.0))


@pytest.fixture
def key(monkeypatch) -> str:
    monkeypatch.setenv(KEY_ENV, "test-key")
    monkeypatch.delenv(USER_IP_ENV, raising=False)
    return "test-key"


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "jobhunt/test"})


def query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# --- parsing -------------------------------------------------------------------


def test_a_search_result_becomes_a_tr_local_snippet_posting() -> None:
    [posting] = CareerjetAdapter().normalize({"pages": [page([JOB])]}, ref())

    assert posting.source == "careerjet"
    assert posting.market == "tr_local"
    assert posting.title == "YAZILIM GELİŞTİRME PERSONELİ"
    assert posting.company_name == "TURKSAT"
    assert posting.location_raw == "Yenimahalle, Ankara"
    # The province, not the district, and Turkey though the text never says so.
    assert posting.city == "Ankara"
    assert posting.country == "TR"
    assert posting.remote_type == "unknown"
    assert "Yazılım geliştirme" in posting.description_text
    assert posting.jd_completeness == "snippet"
    assert posting.apply_url == posting.source_url == JOB["url"]
    assert posting.posted_at is not None and posting.posted_at.day == 2
    assert posting.salary_is_stated is False


def test_the_identity_survives_a_new_tracker_url() -> None:
    """Careerjet hands out a fresh tracker URL for the same job on every call."""
    adapter = CareerjetAdapter()
    [first] = adapter.normalize({"pages": [page([JOB])]}, ref())
    [again] = adapter.normalize({"pages": [page([job(url="https://jobviewtrack.com/v2/other")])]}, ref())
    [elsewhere] = adapter.normalize({"pages": [page([job(locations="Çankaya, Ankara")])]}, ref())

    assert first.external_id == again.external_id
    assert first.external_id != elsewhere.external_id


def test_salary_and_work_mode_are_read_when_stated() -> None:
    paid = job(
        title="Uzaktan Backend Developer", salary="₺60000 - 80000", salary_min=60000.0,
        salary_max=80000.0, salary_currency_code="TRY", salary_type="M",
    )
    [posting] = CareerjetAdapter().normalize({"pages": [page([paid])]}, ref())

    assert (posting.salary_min, posting.salary_max) == (60000.0, 80000.0)
    assert (posting.salary_currency, posting.salary_period) == ("TRY", "monthly")
    assert posting.salary_is_stated is True
    assert posting.remote_type == "remote"


def test_a_location_answer_and_junk_yield_nothing() -> None:
    raw = {"pages": [
        {"type": "LOCATIONS", "locations": ["Ankara", "Ankara (Merkez)"], "message": "multiple locations found"},
        page([{"title": "no company"}, "junk", JOB, JOB]),
    ]}
    assert len(list(CareerjetAdapter().normalize(raw, ref()))) == 1


def test_the_job_type_filter_is_the_employment_type() -> None:
    [posting] = CareerjetAdapter().normalize({"pages": [page([JOB])]}, ref(work_hours="f"))
    assert posting.employment_type == "full_time"


# --- the query -----------------------------------------------------------------


def test_only_turkish_cities_are_sent_as_a_location() -> None:
    assert search_location("İstanbul") == "İstanbul"
    assert search_location("Izmir, Turkey") == "Izmir"
    assert search_location("Turkey") == ""
    assert search_location("Remote") == ""
    assert search_location("Germany") == ""


def test_refs_are_one_search_per_title_and_turkish_city() -> None:
    prefs = prefs_module.Preferences(
        titles=["Backend Engineer", "Data Engineer"],
        locations=["Istanbul", "Turkey", "Remote"],
        job_types=["full_time"],
    )
    refs = CareerjetAdapter().board_refs(prefs)

    assert [r.token for r in refs] == [
        "Backend Engineer|", "Backend Engineer|Istanbul",
        "Data Engineer|", "Data Engineer|Istanbul",
    ]
    assert refs[0].extra == {"work_hours": "f"}


def test_several_job_types_send_no_filter() -> None:
    prefs = prefs_module.Preferences(titles=["QA"], job_types=["full_time", "contract"])
    [only] = CareerjetAdapter().board_refs(prefs)
    assert only.token == "QA|" and only.extra == {}


# --- fetching --------------------------------------------------------------------


def test_the_key_goes_in_basic_auth_and_pages_stop_at_the_last(key) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url).startswith(SEARCH_URL)
        number = int(query(request)["page"][0])
        return httpx.Response(200, json=page([job(title=f"Job {number}")], pages=2))

    raw = CareerjetAdapter().fetch(ref(work_hours="f"), client_for(handler))

    assert len(raw["pages"]) == 2
    expected = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
    assert seen[0].headers["Authorization"] == expected
    params = query(seen[0])
    assert params["locale_code"] == ["tr_TR"]
    assert params["keywords"] == ["Yazılım Geliştirici"]
    assert params["location"] == ["Ankara"]
    assert params["sort"] == ["date"]
    assert params["work_hours"] == ["f"]
    assert params["user_agent"] == ["jobhunt/test"]
    assert params["user_ip"] == ["127.0.0.1"]


def test_a_search_with_no_location_sends_none_and_stops_on_one_page(key) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=page([JOB], pages=1))

    CareerjetAdapter().fetch(ref("QA|"), client_for(handler))

    assert len(seen) == 1
    assert "location" not in query(seen[0])


def test_a_location_answer_ends_the_search(key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "LOCATIONS", "locations": [], "message": "no matching location found"})

    raw = CareerjetAdapter().fetch(ref(), client_for(handler))
    assert len(raw["pages"]) == 1


def test_without_a_key_the_fetch_says_so_and_sends_nothing(monkeypatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request without a key")

    adapter = CareerjetAdapter()
    with pytest.raises(MissingApiKey, match=KEY_ENV):
        adapter.fetch(ref(), client_for(handler))
    assert adapter.still_fetching() is False


def test_a_429_resumes_from_the_refused_page(key) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(query(request)["page"][0])
        calls.append(number)
        if number == 2 and calls.count(2) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=page([job(title=f"Job {number}")], pages=2))

    adapter = CareerjetAdapter()
    with pytest.raises(httpx.HTTPStatusError):
        adapter.fetch(ref(), client_for(handler))
    raw = adapter.fetch(ref(), client_for(handler))

    assert calls == [1, 2, 2]
    assert [p["jobs"][0]["title"] for p in raw["pages"]] == ["Job 1", "Job 2"]
