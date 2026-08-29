"""Preferences to filter rules. The wizard collects answers; this does the writing.

A model hand-writing YAML is how one bad indent silently drops every job, so the
translation lives here and is tested rather than being generated per run.
"""
from __future__ import annotations

import pytest
import yaml

from jobhunt import preferences
from jobhunt.preferences import PreferenceError, Preferences


def set_prefs(cfg, **updates):
    prefs, _ = preferences.load(cfg)
    prefs = preferences.apply_updates(prefs, updates)
    preferences.save(cfg, prefs)
    return prefs


# --- parsing what a person types ----------------------------------------------


def test_lists_are_comma_separated(cfg) -> None:
    prefs = set_prefs(cfg, titles="backend, AI , platform")
    assert prefs.titles == ["backend", "AI", "platform"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("80000", 80000.0), ("80,000", 80000.0), ("80k", 80000.0), ("$80k", 80000.0)],
)
def test_salary_accepts_how_people_write_money(cfg, raw, expected) -> None:
    assert set_prefs(cfg, min_salary=raw).min_salary == expected


def test_experience_accepts_a_trailing_plus_and_aliases(cfg) -> None:
    assert set_prefs(cfg, experience="senior+").experience_min == "senior"
    assert set_prefs(cfg, experience="Entry").experience_min == "junior"


def test_job_types_go_through_the_same_normalizer_as_the_adapters(cfg) -> None:
    assert set_prefs(cfg, job_types="Full-time, internship").job_types == [
        "full_time", "internship"
    ]


def test_clearing_a_setting(cfg) -> None:
    set_prefs(cfg, titles="backend", min_salary="80k")
    prefs = set_prefs(cfg, titles="any", min_salary="none")
    assert prefs.titles == []
    assert prefs.min_salary is None


# --- errors the user can act on -----------------------------------------------


def test_an_unknown_setting_is_a_loud_error(cfg) -> None:
    with pytest.raises(PreferenceError) as exc:
        set_prefs(cfg, colour="blue")
    assert "unknown setting" in str(exc.value)


def test_an_unplaceable_location_names_itself(cfg) -> None:
    """Guessing would filter the corpus to nothing with no visible cause."""
    with pytest.raises(PreferenceError) as exc:
        set_prefs(cfg, locations="Atlantis")
    assert "Atlantis" in str(exc.value)


def test_an_inverted_experience_range_is_refused(cfg) -> None:
    with pytest.raises(PreferenceError):
        set_prefs(cfg, experience="staff", experience_max="mid")


def test_unknown_work_model_and_job_type_are_refused(cfg) -> None:
    with pytest.raises(PreferenceError):
        set_prefs(cfg, work_model="telepathic")
    with pytest.raises(PreferenceError):
        set_prefs(cfg, job_types="volunteering")


# --- translation --------------------------------------------------------------


def test_titles_become_one_escaped_word_bounded_pattern() -> None:
    patterns = preferences.title_patterns(["backend", "node.js", "C++"])
    assert len(patterns) == 1
    import re

    compiled = re.compile(patterns[0])
    assert compiled.search("Senior Backend Engineer")
    assert compiled.search("Node.js Developer")
    assert compiled.search("C++ Engineer")
    # The dot must not act as a wildcard, or "nodexjs" would match.
    assert not compiled.search("nodexjs Developer")


def test_preferences_become_the_expected_rules() -> None:
    prefs = Preferences(
        titles=["backend"], locations=["Turkey", "Germany"], work_model=["remote"],
        job_types=["full_time"], experience_min="senior", min_salary=90000,
        currency="EUR", top_n=10,
    )
    filters = preferences.to_filters(prefs, markets=("global_remote",))
    profile = filters["profiles"]["global_remote"]
    assert profile["hard_requires"] == {
        "remote_type": ["remote"], "employment_type": ["full_time"], "country": ["DE", "TR"],
    }
    assert profile["seniority_min"] == "senior"
    assert profile["salary"]["min_annual"] == 90000
    assert profile["salary"]["currency"] == "EUR"
    assert filters["digest"]["limit"] == 10


def test_worldwide_becomes_an_allowance_not_a_country(cfg) -> None:
    prefs = Preferences(locations=["remote worldwide", "Turkey"])
    profile = preferences.to_filters(prefs, markets=("global_remote",))["profiles"]["global_remote"]
    assert profile["hard_requires"]["country"] == ["TR"]
    assert profile["allow_worldwide"] is True


def test_the_turkey_market_keeps_its_own_country(cfg) -> None:
    """tr_local is Turkey by definition. Overriding it with a global location
    preference would empty the market."""
    prefs = Preferences(locations=["Germany"])
    filters = preferences.to_filters(prefs, markets=("global_remote", "tr_local"))
    assert "country" not in filters["profiles"].get("tr_local", {}).get("hard_requires", {})
    assert filters["profiles"]["global_remote"]["hard_requires"]["country"] == ["DE"]


# --- the file -----------------------------------------------------------------


def test_settings_round_trip_through_the_file(cfg) -> None:
    set_prefs(cfg, titles="backend,AI", work_model="remote,hybrid", experience="senior",
              job_types="full_time", min_salary="100k", top_n="12")
    prefs, _ = preferences.load(cfg)
    assert prefs.titles == ["backend", "AI"]
    assert prefs.work_model == ["remote", "hybrid"]
    assert prefs.experience_min == "senior"
    assert prefs.min_salary == 100000.0
    assert prefs.top_n == 12


def test_hand_edits_outside_the_managed_block_survive(cfg) -> None:
    set_prefs(cfg, titles="backend")
    path = preferences.filters_path(cfg)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["profiles"]["global_remote"]["hard_excludes"] = ["night shift"]
    document["profiles"]["global_remote"]["min_score_to_surface"] = 0.8
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    set_prefs(cfg, work_model="remote")

    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile = after["profiles"]["global_remote"]
    assert profile["hard_excludes"] == ["night shift"]
    assert profile["min_score_to_surface"] == 0.8
    assert profile["hard_requires"]["remote_type"] == ["remote"]


def test_a_write_is_atomic(cfg) -> None:
    """An interrupted write must not leave a half-file that fails to parse."""
    set_prefs(cfg, titles="backend")
    path = preferences.filters_path(cfg)
    assert not path.with_suffix(".yaml.tmp").exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_written_file_is_what_the_filter_actually_loads(cfg) -> None:
    """The end-to-end guarantee: what the wizard writes is what ranking reads."""
    from jobhunt.rank import deterministic

    set_prefs(cfg, titles="backend", work_model="remote", experience="senior")
    filters = deterministic.load_filters(cfg)
    profile = deterministic.profile_for(filters, "global_remote")
    assert profile["hard_requires"]["remote_type"] == ["remote"]
    assert profile["seniority_min"] == "senior"


def test_display_reports_no_restriction_as_any(cfg) -> None:
    rows = dict(Preferences().display())
    assert rows["titles"] == "any"
    assert rows["min salary"] == "any"
    assert rows["show me"] == "15 per run"


def test_loading_a_config_does_not_mutate_the_defaults() -> None:
    """A shallow merge leaves untouched subsections aliased to DEFAULT_CONFIG, so
    writing to one loaded config changes every later one in the process."""
    from jobhunt import config as config_module

    first = config_module._merge(config_module.DEFAULT_CONFIG, {})
    first["sync"]["max_boards_per_run"] = 1
    first["ranking"]["profile_summary"] = "leaked"

    second = config_module._merge(config_module.DEFAULT_CONFIG, {})
    assert second["sync"]["max_boards_per_run"] == 200
    assert second["ranking"]["profile_summary"] is None
    assert config_module.DEFAULT_CONFIG["sync"]["max_boards_per_run"] == 200


# --- saved title groups -------------------------------------------------------
#
# A group is a named selection of titles the user can pick again after clearing
# the field. It rides in the managed block beside the titles themselves, so
# there is no second file to keep in step.


def test_a_saved_group_survives_a_round_trip(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs = preferences.set_group(prefs, "test", ["backend engineer", "platform engineer"])
    preferences.save(cfg, prefs)

    reloaded, _ = preferences.load(cfg)

    assert reloaded.title_groups == {"test": ["backend engineer", "platform engineer"]}


def test_saving_a_group_leaves_the_titles_alone(cfg) -> None:
    prefs = set_prefs(cfg, titles="data engineer")
    prefs = preferences.set_group(prefs, "test", ["backend engineer"])
    preferences.save(cfg, prefs)

    reloaded, _ = preferences.load(cfg)

    assert reloaded.titles == ["data engineer"]


def test_a_group_saved_under_an_existing_name_replaces_it(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs = preferences.set_group(prefs, "test", ["backend engineer"])
    prefs = preferences.set_group(prefs, "TEST", ["data engineer"])

    assert prefs.title_groups == {"TEST": ["data engineer"]}


def test_a_group_needs_a_name(cfg) -> None:
    prefs, _ = preferences.load(cfg)

    with pytest.raises(PreferenceError):
        preferences.set_group(prefs, "   ", ["backend engineer"])


def test_a_group_needs_at_least_one_title(cfg) -> None:
    prefs, _ = preferences.load(cfg)

    with pytest.raises(PreferenceError):
        preferences.set_group(prefs, "test", [])


def test_a_group_keeps_the_first_spelling_of_a_repeated_title(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs = preferences.set_group(prefs, "test", ["Backend Engineer", "backend engineer"])

    assert prefs.title_groups["test"] == ["Backend Engineer"]


def test_deleting_a_group_removes_only_that_one(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs = preferences.set_group(prefs, "test", ["backend engineer"])
    prefs = preferences.set_group(prefs, "ml", ["ml engineer"])

    prefs = preferences.delete_group(prefs, "test")

    assert list(prefs.title_groups) == ["ml"]


def test_deleting_a_group_that_is_not_there_is_refused(cfg) -> None:
    prefs, _ = preferences.load(cfg)

    with pytest.raises(PreferenceError):
        preferences.delete_group(prefs, "nope")
