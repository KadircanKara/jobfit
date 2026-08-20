"""Adapter tests against payloads captured from the live endpoints on 2026-08-20.

Fixtures are real responses, trimmed to five jobs. Testing against a hand-written
shape would only prove the adapter matches my assumptions, not the API.
"""
from __future__ import annotations

import pytest
from conftest import load_fixture

from jobhunt.sources.ashby import AshbyAdapter
from jobhunt.sources.base import BoardRef
from jobhunt.sources.greenhouse import GreenhouseAdapter


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
