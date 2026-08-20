"""Tier 2 aggregator adapters, against real captured payloads. No network.

Every fixture is a real response trimmed to four records, so these tests fail
when the API changes shape rather than when my assumptions change.
"""
from __future__ import annotations

from conftest import FIXTURES, load_fixture

from jobhunt.sources.arbeitnow import ArbeitnowAdapter
from jobhunt.sources.base import BoardRef
from jobhunt.sources.jobicy import JobicyAdapter
from jobhunt.sources.remoteok import RemoteOkAdapter
from jobhunt.sources.remotive import RemotiveAdapter
from jobhunt.sources.wwr import WeWorkRemotelyAdapter

# --- remotive -----------------------------------------------------------------


def remotive_postings() -> list:
    raw = load_fixture("remotive_all.json")
    return list(RemotiveAdapter().normalize(raw, BoardRef("remotive", "all")))


def test_remotive_normalizes_the_feed() -> None:
    postings = remotive_postings()
    assert len(postings) == 4
    assert all(p.remote_type == "remote" for p in postings)
    assert all(p.jd_completeness == "full" for p in postings)


def test_remotive_parses_a_k_suffixed_salary_string() -> None:
    posting = next(p for p in remotive_postings() if p.salary_min == 36000.0)
    assert posting.salary_max is None
    assert posting.salary_currency == "USD"
    assert posting.salary_period == "annual"


def test_remotive_parses_a_salary_range() -> None:
    posting = next(p for p in remotive_postings() if p.salary_min == 150000.0)
    assert posting.salary_max == 230000.0


def test_remotive_ignores_hourly_rates_rather_than_calling_them_annual() -> None:
    """"$120 - $170 /hour" is not a salary band. A wrong number is worse than none."""
    hourly = [p for p in remotive_postings() if "hour" in (p.title + str(p.location_raw)).lower()]
    del hourly  # the guard is on every posting, not just that one
    for posting in remotive_postings():
        if posting.salary_is_stated:
            assert posting.salary_min >= 1000


# --- remoteok -----------------------------------------------------------------


def remoteok_postings() -> list:
    raw = load_fixture("remoteok_all.json")
    return list(RemoteOkAdapter().normalize(raw, BoardRef("remoteok", "all")))


def test_remoteok_skips_the_legal_notice_element() -> None:
    """The first array element is a notice, not a job. PLAN.md warns about it."""
    raw = load_fixture("remoteok_all.json")
    assert "legal" in raw[0]
    postings = remoteok_postings()
    assert len(postings) == len(raw) - 1
    assert all(p.title for p in postings)


def test_remoteok_zero_salary_is_not_stated() -> None:
    """0 means "not stated" here, not a free job."""
    for posting in remoteok_postings():
        assert posting.salary_is_stated is False
        assert posting.salary_min is None


def test_remoteok_epoch_becomes_a_date() -> None:
    posting = remoteok_postings()[0]
    assert posting.posted_at is not None
    assert 2020 <= posting.posted_at.year <= 2030


def test_remoteok_notice_only_payload_yields_nothing() -> None:
    adapter = RemoteOkAdapter()
    assert list(adapter.normalize([{"legal": "..."}], BoardRef("remoteok", "all"))) == []
    assert list(adapter.normalize({"jobs": []}, BoardRef("remoteok", "all"))) == []


# --- arbeitnow ----------------------------------------------------------------


def arbeitnow_postings() -> list:
    raw = load_fixture("arbeitnow_all.json")
    return list(ArbeitnowAdapter().normalize(raw, BoardRef("arbeitnow", "all")))


def test_arbeitnow_unescapes_the_description() -> None:
    """The body arrives as &lt;div&gt;, exactly like Greenhouse."""
    raw = load_fixture("arbeitnow_all.json")
    assert raw["data"][0]["description"].startswith("&lt;")
    posting = arbeitnow_postings()[0]
    assert "<" in posting.description_html
    assert "&lt;" not in posting.description_text
    assert posting.jd_completeness == "full"


def test_arbeitnow_empty_location_stays_unknown() -> None:
    """An empty string is not a location, and inventing one poisons dedupe."""
    postings = arbeitnow_postings()
    blank = [p for p in postings if p.location_raw is None]
    assert blank
    assert all(p.country is None and p.city is None for p in blank)


def test_arbeitnow_uses_the_slug_as_the_external_id() -> None:
    assert all(p.external_id and not p.external_id.isdigit() for p in arbeitnow_postings())


# --- jobicy -------------------------------------------------------------------


def jobicy_postings() -> list:
    raw = load_fixture("jobicy_all.json")
    return list(JobicyAdapter().normalize(raw, BoardRef("jobicy", "all")))


def test_jobicy_reads_the_structured_salary() -> None:
    posting = jobicy_postings()[0]
    assert posting.salary_is_stated
    assert posting.salary_currency == "USD"
    assert posting.salary_period == "annual"
    assert posting.salary_min < posting.salary_max


def test_jobicy_titles_and_descriptions_are_complete() -> None:
    postings = jobicy_postings()
    assert len(postings) == 4
    assert all(p.title for p in postings)
    assert all(p.jd_completeness == "full" for p in postings)


# --- we work remotely ---------------------------------------------------------


def wwr_postings() -> list:
    raw = (FIXTURES / "wwr_programming.xml").read_text(encoding="utf-8")
    return list(WeWorkRemotelyAdapter().normalize(raw, BoardRef("wwr", "remote-programming-jobs")))


def test_wwr_parses_the_rss_feed() -> None:
    postings = wwr_postings()
    assert len(postings) == 4
    assert all(p.external_id.startswith("https://") for p in postings)


def test_wwr_splits_company_from_title() -> None:
    postings = wwr_postings()
    assert all(p.company_name != "unknown" for p in postings)
    assert all(not p.title.startswith(p.company_name) for p in postings)


def test_wwr_title_without_a_colon_keeps_itself_and_reports_no_company() -> None:
    """Inventing a company name from a title is worse than admitting ignorance."""
    adapter = WeWorkRemotelyAdapter()
    assert adapter._split_title("Senior Engineer") == ("unknown", "Senior Engineer")
    assert adapter._split_title(": Engineer") == ("unknown", ": Engineer")
    assert adapter._split_title("Acme: Engineer") == ("Acme", "Engineer")


def test_wwr_malformed_feed_yields_nothing_instead_of_raising() -> None:
    adapter = WeWorkRemotelyAdapter()
    assert list(adapter.normalize("<not xml", BoardRef("wwr", "all"))) == []
    assert list(adapter.normalize("", BoardRef("wwr", "all"))) == []
    assert list(adapter.normalize(None, BoardRef("wwr", "all"))) == []
