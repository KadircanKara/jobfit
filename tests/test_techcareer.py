"""Techcareer.net's job list API. No network: the HTTP client is a MockTransport.

The fixture is four jobs from a live job-list response (2026-10-04), with the
descriptions cut short.
"""
from __future__ import annotations

import copy
import json
from urllib.parse import unquote

import httpx
import pytest
from conftest import FIXTURES

from jobhunt.sources.base import BoardRef, RateLimit
from jobhunt.sources.techcareer import LIST_URL, MAX_PAGES, TechcareerAdapter

PAGE = json.loads((FIXTURES / "techcareer_job_list.json").read_text())
REF = BoardRef(provider="techcareer", token="all", market="tr_local")


def postings(page: dict | None = None) -> dict[str, object]:
    raw = {"pages": [page or PAGE]}
    return {p.external_id: p for p in TechcareerAdapter().normalize(raw, REF)}


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch) -> None:
    monkeypatch.setattr(TechcareerAdapter, "rate_limit", RateLimit(0.0))


# --- parsing -------------------------------------------------------------------


def test_a_list_item_becomes_a_full_tr_local_posting() -> None:
    posting = postings()["9516"]

    assert posting.source == "techcareer"
    assert posting.market == "tr_local"
    assert posting.title == "Yazılım Geliştirme Elemanı"
    assert posting.company_name.startswith("YFA Furkan Altınkaynak")
    assert posting.location_raw == "Ankara / Türkiye"
    assert (posting.country, posting.city) == ("TR", "Ankara")
    assert posting.remote_type == "onsite"
    assert posting.jd_completeness == "full"
    assert "YFA Motion Pictures" in posting.description_text
    url = "https://www.techcareer.net/jobs/detail/yazlm-gelistirme-eleman-4496171"
    assert posting.source_url == posting.apply_url == url


def test_a_side_of_istanbul_still_reads_as_istanbul_and_hybrid_maps() -> None:
    posting = postings()["10107"]
    assert (posting.country, posting.city) == ("TR", "İstanbul")
    assert posting.remote_type == "hybrid"


def test_a_hidden_employer_takes_the_boards_label() -> None:
    assert postings()["10106"].company_name == "Gizli Firma"


def test_an_external_apply_link_is_the_apply_url() -> None:
    posting = postings()["9678"]
    assert posting.apply_url.startswith("https://www.kockariyerim.com/jobAd/")
    assert posting.source_url.startswith("https://www.techcareer.net/jobs/detail/")


def test_the_most_flexible_work_place_wins() -> None:
    page = copy.deepcopy(PAGE)
    page["jobs"][0]["workPlaces"] = [
        {"workPlaceId": 1, "workPlaceName": "İş Yerinde"},
        {"workPlaceId": 2, "workPlaceName": "Uzaktan"},
    ]
    assert postings(page)["9516"].remote_type == "remote"


def test_disabled_unpublished_and_junk_items_are_skipped() -> None:
    page = copy.deepcopy(PAGE)
    page["jobs"][0]["isDisabledJob"] = True
    page["jobs"][1]["jobStatus"] = "Closed"
    page["jobs"] += ["junk", {"jobId": 1}, copy.deepcopy(PAGE["jobs"][2])]
    assert set(postings(page)) == {"10106", "9678"}


# --- fetching --------------------------------------------------------------------


def page_of(number: int, count: int) -> dict:
    item = copy.deepcopy(PAGE["jobs"][0])
    item["jobId"] = number
    return {**PAGE, "jobs": [item], "pageNumber": number, "pageCount": count}


def test_every_page_is_read_with_the_bracketed_parameter() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = unquote(str(request.url))
        seen.append(url)
        number = int(url.rsplit("=", 1)[1])
        return httpx.Response(200, json=page_of(number, 3))

    raw = TechcareerAdapter().fetch(REF, httpx.Client(transport=httpx.MockTransport(handler)))

    assert seen == [f"{LIST_URL}?jobs[page]={n}" for n in (1, 2, 3)]
    assert len(list(TechcareerAdapter().normalize(raw, REF))) == 3


def test_an_empty_page_ends_the_read_and_a_runaway_count_is_capped() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**PAGE, "jobs": [], "pageCount": 10})

    raw = TechcareerAdapter().fetch(REF, httpx.Client(transport=httpx.MockTransport(empty)))
    assert len(raw["pages"]) == 1

    def runaway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page_of(1, 10_000))

    raw = TechcareerAdapter().fetch(REF, httpx.Client(transport=httpx.MockTransport(runaway)))
    assert len(raw["pages"]) == MAX_PAGES


def test_techcareer_is_registered_as_a_tr_local_feed() -> None:
    from jobhunt.discovery import feeds

    assert ("techcareer", "all", "tr_local") in feeds.FEEDS
