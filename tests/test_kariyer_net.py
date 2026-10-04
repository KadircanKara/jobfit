"""kariyer.net. No network: the HTTP client is a MockTransport.

The fixtures are trimmed from live pages (2026-10-04): three cards of a
`/is-ilanlari?kw=python` search (one on-site, one remote, one hybrid) and the
description block of one posting page.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import httpx
import pytest

from jobhunt import preferences as prefs_module
from jobhunt import sync
from jobhunt.preferences import Preferences
from jobhunt.sources import kariyer_net_query as query
from jobhunt.sources.base import BoardRef, RateLimit
from jobhunt.sources.kariyer_net import KariyerNetAdapter, parse_cards
from jobhunt.web.app import board_sources

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "kariyer_net_search.html").read_text()
DETAIL = (FIXTURES / "kariyer_net_detail.html").read_text()
NOW = dt.datetime(2026, 10, 4, 12, 0)

ONSITE, REMOTE, HYBRID = "4569814", "4564050", "4294404"


def adapter(**kwargs) -> KariyerNetAdapter:
    built = KariyerNetAdapter(now=lambda: NOW, **kwargs)
    built.rate_limit = RateLimit(0)
    return built


def ref(**params) -> BoardRef:
    return BoardRef(
        provider="kariyer_net", token="python", market="tr_local",
        extra={"params": {"kw": "python", **params}},
    )


# --- query ---------------------------------------------------------------------------


def test_an_unfiltered_search_sends_only_the_keyword() -> None:
    assert query.search_params("python", Preferences(max_age_days=0)) == {"kw": "python"}


def test_work_model_maps_to_the_site_codes_and_all_three_send_nothing() -> None:
    both = Preferences(work_model=["remote", "hybrid"], max_age_days=0)
    assert query.search_params("x", both)["wm"] == "1,2"
    assert query.search_params("x", Preferences(work_model=["onsite"], max_age_days=0))["wm"] == "0"
    every = Preferences(work_model=["remote", "hybrid", "onsite"], max_age_days=0)
    assert "wm" not in query.search_params("x", every)


def test_contract_asks_for_project_based_and_freelance_work() -> None:
    params = query.search_params("x", Preferences(job_types=["full_time", "contract"], max_age_days=0))
    assert params["tpst"] == "1,2,5"
    assert query.search_params("x", Preferences(job_types=["part_time"], max_age_days=0))["tpst"] == "4"


@pytest.mark.parametrize(
    ("days", "code"),
    [(1, "1g"), (2, "3g"), (3, "3g"), (5, "7g"), (10, "15g"), (15, "15g"), (30, None)],
)
def test_the_age_rounds_up_to_the_next_bucket_never_down(days, code) -> None:
    assert query.search_params("x", Preferences(max_age_days=days)).get("date") == code


def test_a_seniority_band_becomes_position_levels() -> None:
    prefs = Preferences(experience_min="junior", experience_max="senior", max_age_days=0)
    assert query.search_params("x", prefs)["lpst"] == "5,9,4"
    assert "lpst" not in query.search_params("x", Preferences(max_age_days=0))


def test_an_internship_job_type_adds_the_intern_level_to_a_band() -> None:
    prefs = Preferences(experience_min="mid", job_types=["internship"], max_age_days=0)
    assert query.search_params("x", prefs)["lpst"].split(",")[-1] == "8"


def test_only_turkish_cities_narrow_the_search() -> None:
    assert query.city_ids(["İstanbul", "Germany", "izmir, Turkey", "Remote"]) == ["998", "35"]
    assert query.city_ids(["Berlin"]) == []


def test_later_pages_carry_the_page_number() -> None:
    assert "cp" not in query.search_params("x", Preferences(max_age_days=0))
    assert query.search_params("x", Preferences(max_age_days=0), page=2)["cp"] == "2"


def test_board_refs_are_one_per_title_with_the_filters_built_in() -> None:
    prefs = Preferences(titles=["Backend Engineer", "Data Engineer"], locations=["Ankara"],
                        work_model=["remote"], max_age_days=7)
    refs = KariyerNetAdapter().board_refs(prefs)
    assert [r.token for r in refs] == ["Backend Engineer", "Data Engineer"]
    assert refs[0].extra["params"] == {"kw": "Backend Engineer", "wm": "1", "date": "7g", "ct": "6"}
    assert refs[0].market == "tr_local"


# --- parse and normalize -------------------------------------------------------------


def test_cards_are_read_from_the_search_page() -> None:
    cards = parse_cards(SEARCH)
    assert [c["id"] for c in cards] == [ONSITE, REMOTE, HYBRID]
    assert cards[1]["title"] == "Biyoistatistik Uzmanı - Databiyo - Uzaktan Çalışma"
    assert cards[1]["work_model"] == "Uzaktan / Remote"


def test_a_card_normalizes_with_its_work_model_type_and_age() -> None:
    raw = {"fetched_at": NOW.isoformat(), "pages": [SEARCH], "details": {}}
    jobs = {j.external_id: j for j in adapter().normalize(raw, ref())}

    onsite = jobs[ONSITE]
    assert onsite.title == "Bilgi Teknolojileri / IT Uzmanı"
    assert onsite.company_name.startswith("DENKA")
    assert (onsite.country, onsite.city, onsite.remote_type) == ("TR", "Kayseri", "onsite")
    assert onsite.employment_type == "full_time"
    assert onsite.posted_at == NOW - dt.timedelta(days=2)
    assert onsite.source_url == query.BASE_URL + "/is-ilani/" + (
        "denka-bilgi-teknolojileri-ve-haberlesme-sitemleri-bilgi-teknolojileri-it-uzmani-4569814"
    )
    assert onsite.jd_completeness == "none"
    assert jobs[REMOTE].remote_type == "remote"
    assert jobs[HYBRID].remote_type == "hybrid"


def test_a_fetched_posting_page_supplies_the_description() -> None:
    raw = {"fetched_at": NOW.isoformat(), "pages": [SEARCH], "details": {ONSITE: DETAIL}}
    job = next(j for j in adapter().normalize(raw, ref()) if j.external_id == ONSITE)
    assert "Genel Nitelikler" in job.description_text
    assert job.jd_source == "html"
    assert job.jd_completeness != "none"


def test_a_card_repeated_on_the_next_page_is_one_job() -> None:
    raw = {"fetched_at": NOW.isoformat(), "pages": [SEARCH, SEARCH], "details": {}}
    assert len(list(adapter().normalize(raw, ref()))) == 3


def test_a_multi_city_posting_keeps_the_first_city() -> None:
    page = SEARCH.replace(">Kayseri<", ">İstanbul(Asya) +2 il daha<")
    raw = {"fetched_at": NOW.isoformat(), "pages": [page], "details": {}}
    job = next(j for j in adapter().normalize(raw, ref()) if j.external_id == ONSITE)
    assert job.city == "İstanbul"


# --- fetch ---------------------------------------------------------------------------


def serve(search_pages: list[str], detail_status: int = 200):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/is-ilanlari":
            page = int(request.url.params.get("cp", "1"))
            return httpx.Response(200, text=search_pages[min(page, len(search_pages)) - 1])
        return httpx.Response(detail_status, text=DETAIL)

    return handler, calls


def no_title_rules(monkeypatch) -> None:
    from jobhunt import board_scope

    monkeypatch.setattr(board_scope.deterministic, "load_filters", lambda config: {"global": {}})


def test_a_short_page_is_the_last_one(cfg, monkeypatch) -> None:
    no_title_rules(monkeypatch)
    handler, calls = serve([SEARCH])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter(config=cfg).fetch(ref(wm="1"), client)

    searches = [c for c in calls if c.url.path == "/is-ilanlari"]
    assert len(searches) == 1
    assert searches[0].url.params["wm"] == "1"
    assert len(raw["pages"]) == 1
    assert set(raw["details"]) == {ONSITE, REMOTE, HYBRID}


def test_detail_pages_skip_known_ids_and_titles_stage_1_drops(cfg, monkeypatch) -> None:
    from jobhunt import board_scope

    monkeypatch.setattr(
        board_scope.deterministic, "load_filters",
        lambda config: {"global": {"require_titles_regex": ["(?i)uzman"]}},
    )
    handler, calls = serve([SEARCH])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapter(config=cfg, known_ids={REMOTE}).fetch(ref(), client)

    # The hybrid card's title has no "uzman"; the remote one is already stored.
    assert set(raw["details"]) == {ONSITE}
    assert {j.external_id for j in adapter().normalize(raw, ref())} == {ONSITE, REMOTE, HYBRID}


def test_a_403_on_a_posting_page_stops_details_and_marks_the_fetch_truncated(cfg, monkeypatch) -> None:
    no_title_rules(monkeypatch)
    handler, calls = serve([SEARCH], detail_status=403)
    built = adapter(config=cfg)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = built.fetch(ref(), client)

    assert raw["details"] == {}
    assert len([c for c in calls if c.url.path != "/is-ilanlari"]) == 1
    assert built.was_truncated()


def test_a_429_on_search_raises_for_the_sync_loop_to_wait_out(cfg) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            adapter(config=cfg).fetch(ref(), client)


# --- wiring --------------------------------------------------------------------------


def test_the_adapter_is_built_with_refs_and_the_corpus(cfg) -> None:
    prefs, _ = prefs_module.load(cfg)
    prefs.titles = ["Backend Engineer"]
    prefs.locations = ["Istanbul"]
    prefs_module.save(cfg, prefs)

    built = sync.build_adapter(cfg, "kariyer_net")

    assert built.config is cfg
    [only] = list(built.discover())
    assert only.token == "Backend Engineer"
    assert only.extra["params"]["ct"] == "998"


def test_it_is_a_search_not_a_board_source() -> None:
    assert "kariyer_net" not in board_sources()
