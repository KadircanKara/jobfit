"""The web layer's filter adapter.

The CLI already validates most of this in preferences.apply_updates. These
tests cover what the web form adds on top: typed JSON in, a unit on max age,
bounds the CLI does not enforce, and errors tagged with the field that caused
them so the form can mark the right input.
"""
from __future__ import annotations

import pytest

from jobhunt.preferences import Preferences
from jobhunt.web import filters as webfilters


def test_max_age_in_weeks_becomes_days():
    prefs = webfilters.apply(Preferences(), {"max_age": {"value": 2, "unit": "weeks"}})

    assert prefs.max_age_days == 14


def test_max_age_in_hours_rounds_up_to_a_whole_day():
    prefs = webfilters.apply(Preferences(), {"max_age": {"value": 30, "unit": "hours"}})

    assert prefs.max_age_days == 2


def test_max_age_unit_the_form_does_not_offer_is_rejected():
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"max_age": {"value": 3, "unit": "fortnights"}})

    assert exc.value.field == "max_age"


def test_ceiling_below_floor_is_rejected_against_the_experience_field():
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"experience_min": "senior", "experience_max": "mid"})

    assert exc.value.field == "experience_max"
    assert "ceiling" in str(exc.value).lower()


def test_a_location_that_cannot_be_placed_names_itself():
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"locations": ["Germany", "Atlantis"]})

    assert exc.value.field == "locations"
    assert "Atlantis" in str(exc.value)


def test_negative_salary_is_rejected():
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"min_salary": -5})

    assert exc.value.field == "min_salary"


def test_salary_shorthand_expands():
    prefs = webfilters.apply(Preferences(), {"min_salary": "80k"})

    assert prefs.min_salary == 80000


def test_top_n_above_the_cap_is_rejected():
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"top_n": 500})

    assert exc.value.field == "top_n"


def test_titles_are_deduped_case_insensitively_keeping_the_first_spelling():
    prefs = webfilters.apply(Preferences(), {"titles": ["AI Engineer", "ai engineer", "RAG"]})

    assert prefs.titles == ["AI Engineer", "RAG"]


def test_an_empty_title_list_clears_the_rule_rather_than_matching_nothing():
    prefs = webfilters.apply(Preferences(titles=["AI Engineer"]), {"titles": []})

    assert prefs.titles == []


def test_unticking_every_source_is_rejected_rather_than_stored():
    """An empty list flattens to the CLI's "none", which used to store an empty
    selection: the run fetched nothing and the shortlist restricted nothing."""
    with pytest.raises(webfilters.FieldError) as exc:
        webfilters.apply(Preferences(), {"sources": []})

    assert exc.value.field == "sources"
    assert "ats" in str(exc.value)
