"""Adapter tests against payloads captured from the live endpoints on 2026-08-20.

Fixtures are real responses, trimmed to five jobs. Testing against a hand-written
shape would only prove the adapter matches my assumptions, not the API.
"""
from __future__ import annotations

import pytest
from conftest import FIXTURES, load_fixture

from jobhunt.sources.ashby import AshbyAdapter
from jobhunt.sources.base import BoardRef
from jobhunt.sources.greenhouse import GreenhouseAdapter
from jobhunt.sources.lever import LeverAdapter
from jobhunt.sources.personio import PersonioAdapter
from jobhunt.sources.recruitee import RecruiteeAdapter
from jobhunt.sources.smartrecruiters import SmartRecruitersAdapter
from jobhunt.sources.workable import WorkableAdapter


@pytest.fixture
def greenhouse_postings():
    ref = BoardRef("greenhouse", "stripe", "global_remote")
    return list(GreenhouseAdapter().normalize(load_fixture("greenhouse_stripe.json"), ref))


@pytest.fixture
def ashby_postings():
    ref = BoardRef("ashby", "ramp", "global_remote", extra={"company_name": "Ramp"})
    return list(AshbyAdapter().normalize(load_fixture("ashby_ramp.json"), ref))


# --- greenhouse ---------------------------------------------------------------


def test_greenhouse_normalizes_every_job(greenhouse_postings) -> None:
    assert len(greenhouse_postings) == 5
    assert all(p.source == "greenhouse" for p in greenhouse_postings)
    assert all(p.external_id and p.title for p in greenhouse_postings)


def test_greenhouse_unescapes_the_description(greenhouse_postings) -> None:
    """content arrives HTML-escaped. Missing this yields entity noise in every JD."""
    posting = greenhouse_postings[0]
    assert "&lt;" not in posting.description_html
    assert "<" in posting.description_html
    assert "&lt;" not in posting.description_md


def test_greenhouse_descriptions_are_complete(greenhouse_postings) -> None:
    assert all(p.jd_completeness == "full" for p in greenhouse_postings)
    assert all(p.jd_source == "api" for p in greenhouse_postings)


def test_greenhouse_resolves_the_employer_domain_not_the_ats(greenhouse_postings) -> None:
    """Stripe's absolute_url is stripe.com, which is a real employer domain."""
    domains = {p.company_domain for p in greenhouse_postings}
    assert domains == {"stripe.com"}


def test_greenhouse_handles_null_metadata() -> None:
    """metadata is null on most boards, so the location read must not index into it."""
    ref = BoardRef("greenhouse", "acme")
    raw = {"jobs": [{"id": 1, "title": "Backend Engineer", "metadata": None,
                     "location": {"name": "Berlin, Germany"}, "content": "&lt;p&gt;hi&lt;/p&gt;"}]}
    posting = next(iter(GreenhouseAdapter().normalize(raw, ref)))
    assert posting.location_raw == "Berlin, Germany"
    assert posting.country == "DE"


def test_greenhouse_prefers_metadata_location_over_work_mode() -> None:
    ref = BoardRef("greenhouse", "acme")
    raw = {"jobs": [{"id": 2, "title": "Backend Engineer", "location": {"name": "Remote"},
                     "metadata": [{"name": "Job Posting Location", "value": "Istanbul, Turkey"}],
                     "content": "x"}]}
    posting = next(iter(GreenhouseAdapter().normalize(raw, ref)))
    assert posting.location_raw == "Istanbul, Turkey"
    assert posting.country == "TR"


def test_greenhouse_skips_records_with_no_id_or_title() -> None:
    ref = BoardRef("greenhouse", "acme")
    raw = {"jobs": [{"id": None, "title": "X"}, {"id": 3, "title": ""}, {"id": 4, "title": "Ok"}]}
    assert [p.external_id for p in GreenhouseAdapter().normalize(raw, ref)] == ["4"]


# --- ashby --------------------------------------------------------------------


def test_ashby_normalizes_every_listed_job(ashby_postings) -> None:
    assert len(ashby_postings) == 5
    assert all(p.source == "ashby" for p in ashby_postings)


def test_ashby_descriptions_are_complete(ashby_postings) -> None:
    assert all(p.jd_completeness == "full" for p in ashby_postings)


def test_ashby_carries_no_employer_domain(ashby_postings) -> None:
    """Ashby payloads have no company domain, and jobs.ashbyhq.com is the ATS."""
    assert all(p.company_domain is None for p in ashby_postings)


def test_ashby_maps_workplace_type_not_is_remote() -> None:
    """Verified live: ramp returns workplaceType Hybrid with isRemote true.
    Trusting isRemote would mislabel an office job as remote."""
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Engineer", "workplaceType": "Hybrid",
                     "isRemote": True, "location": "New York, NY"}]}
    assert next(iter(AshbyAdapter().normalize(raw, ref))).remote_type == "hybrid"


def test_ashby_skips_unlisted_postings() -> None:
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Hidden", "isListed": False},
                    {"id": "b", "title": "Public", "isListed": True}]}
    assert [p.external_id for p in AshbyAdapter().normalize(raw, ref)] == ["b"]


def test_ashby_parses_structured_compensation() -> None:
    """Read components, not the summary strings. interval is '1 YEAR', not 'year'."""
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Engineer", "compensation": {
        "compensationTiers": [{"components": [
            {"compensationType": "EquityPercentage", "interval": "NONE",
             "currencyCode": None, "minValue": None, "maxValue": None},
            {"compensationType": "Salary", "interval": "1 YEAR",
             "currencyCode": "USD", "minValue": 211400, "maxValue": 290600},
        ]}]}}]}
    posting = next(iter(AshbyAdapter().normalize(raw, ref)))
    assert (posting.salary_min, posting.salary_max) == (211400.0, 290600.0)
    assert (posting.salary_currency, posting.salary_period) == ("USD", "annual")
    assert posting.salary_is_stated is True


def test_ashby_equity_only_is_not_a_stated_salary() -> None:
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Engineer", "compensation": {
        "compensationTiers": [{"components": [
            {"compensationType": "EquityPercentage", "interval": "NONE",
             "currencyCode": None, "minValue": None, "maxValue": None}]}]}}]}
    posting = next(iter(AshbyAdapter().normalize(raw, ref)))
    assert posting.salary_is_stated is False
    assert posting.salary_min is None


def test_ashby_prefers_postal_address_over_the_location_string() -> None:
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Engineer", "location": "New York, NY (HQ)",
                     "address": {"postalAddress": {"addressCountry": "USA",
                                                   "addressLocality": "New York City"}}}]}
    posting = next(iter(AshbyAdapter().normalize(raw, ref)))
    assert (posting.country, posting.city) == ("US", "New York City")


def test_ashby_missing_compensation_is_not_an_error() -> None:
    ref = BoardRef("ashby", "ramp")
    raw = {"jobs": [{"id": "a", "title": "Engineer", "compensation": None}]}
    assert next(iter(AshbyAdapter().normalize(raw, ref))).salary_min is None


def test_both_adapters_tolerate_an_empty_payload() -> None:
    ref_gh = BoardRef("greenhouse", "acme")
    ref_ash = BoardRef("ashby", "acme")
    assert list(GreenhouseAdapter().normalize({}, ref_gh)) == []
    assert list(GreenhouseAdapter().normalize(None, ref_gh)) == []
    assert list(AshbyAdapter().normalize({"jobs": []}, ref_ash)) == []


# --- lever --------------------------------------------------------------------


def lever_postings() -> list:
    raw = load_fixture("lever_matchgroup.json")
    ref = BoardRef("lever", "matchgroup")
    return list(LeverAdapter().normalize(raw, ref))


def test_lever_normalizes_a_bare_array() -> None:
    """The top level is an array, not an object. Assuming otherwise yields zero jobs."""
    postings = lever_postings()
    assert len(postings) == 5
    assert all(p.source == "lever" for p in postings)


def test_lever_reads_the_title_from_text_not_title() -> None:
    titles = {p.title for p in lever_postings()}
    assert "Android Engineer III" in titles


def test_lever_uses_the_iso_country_field_over_the_location_string() -> None:
    posting = next(p for p in lever_postings() if p.title.startswith("Accountant"))
    assert posting.country == "KR"
    assert posting.city == "Seoul"


def test_lever_reads_the_structured_salary_range() -> None:
    posting = next(p for p in lever_postings() if p.title == "Android Engineer III")
    assert (posting.salary_min, posting.salary_max) == (150000.0, 180000.0)
    assert posting.salary_currency == "USD"
    assert posting.salary_period == "annual"
    assert posting.salary_is_stated


def test_lever_absent_salary_is_not_stated() -> None:
    posting = next(p for p in lever_postings() if p.title.startswith("Accountant"))
    assert posting.salary_is_stated is False
    assert posting.salary_min is None


def test_lever_description_includes_the_list_blocks() -> None:
    """`description` alone truncates the JD on boards that split it into lists."""
    posting = next(p for p in lever_postings() if p.title.startswith("Accountant"))
    assert "Key Responsibilities" in posting.description_html
    assert "purchase orders" in posting.description_text.lower()
    assert posting.jd_completeness == "full"


def test_lever_created_at_is_milliseconds() -> None:
    posting = lever_postings()[0]
    assert posting.posted_at is not None
    assert 2020 <= posting.posted_at.year <= 2030


def test_lever_workplace_type_is_authoritative() -> None:
    assert all(p.remote_type == "hybrid" for p in lever_postings())


def test_lever_apply_url_leaks_its_own_token() -> None:
    """Strategy A closes the loop: a lever posting proves the lever board."""
    from jobhunt.discovery import patterns

    posting = lever_postings()[0]
    hits, _ = patterns.scan(posting.apply_url)
    assert patterns.BoardHit("lever", "matchgroup") in hits


def test_lever_error_body_yields_nothing() -> None:
    assert list(LeverAdapter().normalize({"error": "not found"}, BoardRef("lever", "x"))) == []
    assert list(LeverAdapter().normalize(None, BoardRef("lever", "x"))) == []


# --- recruitee ----------------------------------------------------------------


def recruitee_postings() -> list:
    raw = load_fixture("recruitee_channable.json")
    return list(RecruiteeAdapter().normalize(raw, BoardRef("recruitee", "channable")))


def test_recruitee_normalizes_published_offers() -> None:
    postings = recruitee_postings()
    assert len(postings) == 4
    assert {p.company_name for p in postings} == {"Channable"}


def test_recruitee_concatenates_description_and_requirements() -> None:
    """Either field alone is a partial JD, and a partial JD is a bug."""
    posting = next(p for p in recruitee_postings() if p.title.startswith("Product Manager"))
    assert "Your team" in posting.description_text
    assert len(posting.description_text) > 500
    assert posting.jd_completeness == "full"


def test_recruitee_three_booleans_become_one_mode() -> None:
    posting = next(p for p in recruitee_postings() if p.title.startswith("Product Manager"))
    assert posting.remote_type == "hybrid"


def test_recruitee_salary_arrives_as_strings() -> None:
    posting = next(p for p in recruitee_postings() if p.title.startswith("Product Manager"))
    assert (posting.salary_min, posting.salary_max) == (4500.0, 6000.0)
    assert posting.salary_currency == "EUR"
    assert posting.salary_period == "monthly"


def test_recruitee_uses_the_iso_country_code() -> None:
    posting = recruitee_postings()[0]
    assert posting.country == "NL"
    assert posting.city == "Utrecht"


def test_recruitee_learns_the_employer_domain_from_the_careers_url() -> None:
    """careers_url is the employer's own host, which is a free company domain."""
    posting = recruitee_postings()[0]
    assert posting.company_domain == "jobs.channable.com"


def test_recruitee_unpublished_offers_are_skipped() -> None:
    raw = {"offers": [{"id": 1, "title": "Draft", "status": "draft"}]}
    assert list(RecruiteeAdapter().normalize(raw, BoardRef("recruitee", "x"))) == []


# --- smartrecruiters ----------------------------------------------------------


def smartrecruiters_postings() -> list:
    raw = load_fixture("smartrecruiters_visa.json")
    return list(SmartRecruitersAdapter().normalize(raw, BoardRef("smartrecruiters", "Visa")))


def test_smartrecruiters_reads_name_as_the_title() -> None:
    assert {p.title for p in smartrecruiters_postings()} == {"Sr. Manager", "Director"}


def test_smartrecruiters_never_claims_a_description_it_does_not_have() -> None:
    """The list response carries no description. Calling that a snippet would let
    a partial JD reach the tailoring skill.
    """
    for posting in smartrecruiters_postings():
        assert posting.description_text is None
        assert posting.jd_completeness == "none"
        assert posting.source_url.startswith("https://api.smartrecruiters.com/")


def test_smartrecruiters_flattens_the_location_object() -> None:
    posting = next(p for p in smartrecruiters_postings() if p.title == "Sr. Manager")
    assert posting.city == "Austin"
    assert posting.country == "US"
    assert posting.remote_type == "onsite"


def test_smartrecruiters_apply_url_leaks_its_own_token() -> None:
    from jobhunt.discovery import patterns

    hits, _ = patterns.scan(smartrecruiters_postings()[0].apply_url)
    assert patterns.BoardHit("smartrecruiters", "Visa") in hits


# --- personio -----------------------------------------------------------------


def personio_postings() -> list:
    raw = (FIXTURES / "personio_personio.xml").read_text(encoding="utf-8")
    return list(PersonioAdapter().normalize(raw, BoardRef("personio", "personio")))


def test_personio_parses_the_xml_feed() -> None:
    postings = personio_postings()
    assert postings
    assert all(p.source == "personio" for p in postings)
    assert all(p.external_id.isdigit() for p in postings)


def test_personio_description_joins_the_named_blocks() -> None:
    posting = personio_postings()[0]
    assert posting.jd_completeness == "full"
    assert len(posting.description_text) > 500


def test_personio_builds_the_apply_url_from_the_token_and_id() -> None:
    posting = personio_postings()[0]
    assert posting.apply_url == f"https://personio.jobs.personio.de/job/{posting.external_id}"


def test_personio_keeps_every_office_in_the_location() -> None:
    posting = personio_postings()[0]
    assert "Munich" in posting.location_raw


def test_personio_malformed_feed_yields_nothing_instead_of_raising() -> None:
    assert list(PersonioAdapter().normalize("<not xml", BoardRef("personio", "x"))) == []
    assert list(PersonioAdapter().normalize("", BoardRef("personio", "x"))) == []
    assert list(PersonioAdapter().normalize(None, BoardRef("personio", "x"))) == []


# --- workable -----------------------------------------------------------------


def workable_postings() -> list:
    raw = load_fixture("workable_1kosmos.json")
    return list(WorkableAdapter().normalize(raw, BoardRef("workable", "1kosmos")))


def test_workable_normalizes_the_widget_payload() -> None:
    postings = workable_postings()
    assert len(postings) == 4
    assert all(p.jd_completeness == "full" for p in postings)


def test_workable_company_name_comes_from_the_account_not_the_token() -> None:
    assert {p.company_name for p in workable_postings()} == {"1Kosmos"}


def test_workable_prefers_the_iso_code_in_locations() -> None:
    """The top-level `country` is a display name; locations[] has the real code."""
    posting = workable_postings()[0]
    assert posting.country == "US"
    assert posting.city == "Iselin"


def test_workable_uses_the_shortcode_as_the_external_id() -> None:
    posting = workable_postings()[0]
    assert posting.external_id == "DDE33AFDAC"
    assert posting.apply_url.endswith("/apply")


def test_workable_empty_account_yields_nothing() -> None:
    """Every guessed token in phase 3 answered 200 with this exact shape."""
    adapter = WorkableAdapter()
    assert list(adapter.normalize({"name": "x", "jobs": []}, BoardRef("workable", "x"))) == []
    assert list(adapter.normalize(None, BoardRef("workable", "x"))) == []
