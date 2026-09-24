"""Preferences to filter rules. The wizard collects answers; this does the writing.

A model hand-writing YAML is how one bad indent silently drops every job, so the
translation lives here and is tested rather than being generated per run.
"""
from __future__ import annotations

import dataclasses
import re

import pytest
import yaml

from jobhunt import preferences
from jobhunt.preferences import PreferenceError, Preferences


def set_prefs(cfg, **updates):
    prefs, _ = preferences.load(cfg)
    prefs = preferences.apply_updates(prefs, updates)
    preferences.save(cfg, prefs)
    return prefs


@dataclasses.dataclass
class _Nested:
    number: int = 0
    words: list[str] = dataclasses.field(default_factory=list)


def test_a_nested_dataclass_round_trips_as_a_dataclass(cfg) -> None:
    """A dict assigned where a dataclass belongs raises AttributeError at use."""
    prefs = Preferences(titles=["Backend Engineer"])
    preferences.save(cfg, prefs)
    loaded, _ = preferences.load(cfg)
    # Whatever nested fields exist must come back as their declared type.
    for field in dataclasses.fields(Preferences):
        if dataclasses.is_dataclass(field.type) or dataclasses.is_dataclass(
            getattr(field.type, "__origin__", None)
        ):
            assert not isinstance(getattr(loaded, field.name), dict)


def test_a_dict_is_coerced_into_its_declared_dataclass() -> None:
    coerced = preferences._coerce(_Nested, {"number": 3, "words": ["a"]})
    assert isinstance(coerced, _Nested)
    assert coerced.number == 3 and coerced.words == ["a"]


def test_an_already_correct_value_passes_through() -> None:
    value = _Nested(number=1)
    assert preferences._coerce(_Nested, value) is value


def test_a_dict_of_dataclasses_is_coerced_by_value() -> None:
    out = preferences._coerce(dict[str, _Nested], {"one": {"number": 2}})
    assert isinstance(out["one"], _Nested)


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


def test_sources_go_through_apply_updates(cfg) -> None:
    assert set_prefs(cfg, sources="linkedin, upwork").sources == ["linkedin", "upwork"]


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
    with pytest.raises(PreferenceError):
        set_prefs(cfg, sources="carrier_pigeon")


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


def test_the_upwork_profile_overrides_the_global_title_regex() -> None:
    prefs = Preferences(titles=["Backend Engineer"])
    prefs.upwork.queries = ["rag pipeline", "llm"]
    profile = preferences.to_filters(prefs)["profiles"]["upwork"]
    assert profile["require_titles_regex"] == [".*"]
    assert profile["require_titles_regex"] != preferences.title_patterns_for(prefs)


def test_the_upwork_profile_never_re_filters_on_the_query_titles() -> None:
    """Upwork matched these queries against the whole posting, server-side. A
    title-only approximation of the same queries locally drops gigs Upwork
    itself returned - "AI Engineer for chatbot" contains none of the words in
    the query that found it - and that shows up as a `title_unmatched` bar on a
    source that fetched perfectly."""
    prefs = Preferences(titles=["Backend Engineer"])
    prefs.upwork.queries = ["rag pipeline", "llm", "ai automation"]
    patterns = preferences.to_filters(prefs)["profiles"]["upwork"]["require_titles_regex"]
    assert patterns == [".*"]
    for title in (
        "Build a RAG pipeline for our docs",
        "AI Engineer for chatbot",
        "Need a Django dev to fix my scraper",
        "Automation expert (Make.com)",
    ):
        assert any(re.search(pattern, title) for pattern in patterns), title


def test_an_empty_upwork_query_list_matches_everything() -> None:
    prefs = Preferences(titles=["Backend Engineer"])
    profile = preferences.to_filters(prefs)["profiles"]["upwork"]
    assert profile["require_titles_regex"] == [".*"]


def test_the_upwork_profile_states_no_salary_floor() -> None:
    prefs = Preferences(min_salary=120000)
    assert "salary" not in preferences.to_filters(prefs)["profiles"]["upwork"]


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


# --- seniority-aware title patterns -------------------------------------------
#
# An internship rarely spells out the role the way a permanent posting does:
# "Backend Intern" never contains "Backend Engineer". Setting the experience
# floor to intern makes those jobs eligible, but the title gate is separate, so
# without help they are still dropped as title_unmatched.
#
# The keywords are added to the generated regex only, never to prefs.titles, and
# always in conjunction with a role word the user actually asked for: a bare
# "Intern" alternative matches "Internal Audit Lead", and a bare "Junior" one
# matches "Junior Accountant".


def matched(prefs: Preferences, title: str) -> bool:
    import re as _re

    return any(_re.search(p, title) for p in preferences.title_patterns_for(prefs))


def entry_prefs(**kwargs) -> Preferences:
    base = Preferences(titles=["Software Engineer", "Backend Engineer", "Python Developer"])
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def test_an_intern_floor_finds_an_internship_the_role_titles_miss() -> None:
    prefs = entry_prefs(experience_min="intern")

    assert matched(prefs, "Backend Intern")
    assert matched(prefs, "Software Engineering Intern (Fall 2026)")


def test_the_intern_keyword_never_matches_international_or_internal() -> None:
    prefs = entry_prefs(experience_min="intern")

    assert not matched(prefs, "Internal Audit Data Analytics Lead")
    assert not matched(prefs, "Senior Manager, International Indirect Tax")


def test_an_internship_in_another_field_is_still_refused() -> None:
    prefs = entry_prefs(experience_min="intern")

    assert not matched(prefs, "CNC Machine Park Internship")
    assert not matched(prefs, "Internship Global Supply Chain Management (m/w/d)")


def test_a_junior_floor_finds_a_graduate_posting() -> None:
    prefs = entry_prefs(experience_min="junior")

    assert matched(prefs, "Graduate Software Engineer")
    assert matched(prefs, "New Grad Backend Engineer 2027")


def test_the_junior_keyword_does_not_admit_another_field() -> None:
    prefs = entry_prefs(experience_min="junior")

    assert not matched(prefs, "Junior Accountant")


def test_a_junior_floor_does_not_reach_down_to_internships() -> None:
    prefs = entry_prefs(experience_min="junior")

    assert not matched(prefs, "Backend Intern")


def test_only_the_bottom_of_the_range_gets_keywords(cfg) -> None:
    # A senior floor needs no help: a senior posting spells the role out in full.
    prefs = entry_prefs(experience_min="senior")

    assert preferences.title_patterns_for(prefs) == preferences.title_patterns(prefs.titles)


def test_a_ceiling_below_junior_adds_no_junior_keywords() -> None:
    prefs = entry_prefs(experience_min="intern", experience_max="intern")
    generated = " ".join(preferences.title_patterns_for(prefs))

    assert matched(prefs, "Backend Intern")
    # "Graduate Backend Developer" is out of range, and nothing generated for it.
    assert "graduate" not in generated
    assert not matched(prefs, "Graduate Accountant")


def test_a_level_the_user_already_typed_adds_nothing() -> None:
    typed = Preferences(
        titles=["Software Engineer", "Software Engineering Intern"],
        experience_min="intern",
        experience_max="intern",
    )

    assert preferences.title_patterns_for(typed) == preferences.title_patterns(typed.titles)


def test_the_plain_title_patterns_still_come_first() -> None:
    prefs = entry_prefs(experience_min="intern")
    patterns = preferences.title_patterns_for(prefs)

    assert patterns[0] == preferences.title_patterns(prefs.titles)[0]
    assert matched(prefs, "Senior Backend Engineer")


def test_no_titles_means_no_seniority_patterns_either() -> None:
    # With no titles there is no role side to the conjunction, and a bare
    # seniority keyword would match every internship in every field.
    prefs = Preferences(titles=[], experience_min="intern")

    assert preferences.title_patterns_for(prefs) == []


def test_the_impact_count_includes_what_the_seniority_keywords_add(cfg) -> None:
    # The count under the Titles field is what a person judges the filter by, so
    # it has to count the same patterns the filter will actually run.
    from jobhunt.db.models import Job
    from jobhunt.db.session import session_scope

    with session_scope(cfg.db_path) as session:
        for title in ("Backend Intern", "Warehouse Associate"):
            session.add(
                Job(
                    external_id=title,
                    source="greenhouse",
                    market="global_remote",
                    title=title,
                    title_normalized=title.lower(),
                    is_active=True,
                )
            )

    prefs = Preferences(titles=["Software Engineer", "Backend Engineer"], experience_min="intern")

    assert preferences.title_impact(cfg, prefs) == (1, 2)


def test_the_shipped_filters_do_not_exclude_internships() -> None:
    """Internships are a seniority decision, not a phrase to ban.

    The shipped `yc` profile used to hard-exclude "intern" and "internship" and
    `tr_local` "stajyer", which silently overrode an intern experience floor: the
    phrase fired on the title before seniority was ever consulted. Unpaid work is
    still excluded, which is what those entries were really guarding against.
    """
    import yaml as _yaml

    from jobhunt.rank.deterministic import PACKAGED_FILTERS

    document = _yaml.safe_load(PACKAGED_FILTERS.read_text(encoding="utf-8"))
    banned = {"intern", "internship", "stajyer", "trainee", "working student"}
    for market, profile in (document.get("profiles") or {}).items():
        excluded = {str(p).lower() for p in (profile.get("hard_excludes") or [])}
        assert not (excluded & banned), f"{market} still excludes {excluded & banned}"
    assert "unpaid" in {
        str(p).lower() for p in document["profiles"]["yc"].get("hard_excludes") or []
    }


# --- sources: which corpora a run draws from -----------------------------------


def test_sources_defaults_to_ats_and_linkedin() -> None:
    assert preferences.Preferences().sources == ["ats", "linkedin"]


def test_ats_expands_to_every_existing_adapter() -> None:
    expanded = preferences.adapters_for(["ats"])
    assert "greenhouse" in expanded and "workable" in expanded
    assert "linkedin" not in expanded


def test_linkedin_only_expands_to_linkedin_alone() -> None:
    assert preferences.adapters_for(["linkedin"]) == ["linkedin"]


def test_upwork_expands_to_the_upwork_adapter() -> None:
    assert preferences.adapters_for(["upwork"]) == ["upwork"]


def test_ats_still_excludes_upwork() -> None:
    """Forward-looking guard: passes trivially today since REGISTRY has no
    upwork entry yet, but will start meaning something the moment Task 6
    registers it. The test below is the one that actually bites now."""
    assert "upwork" not in preferences.adapters_for(["ats"])


def test_ats_group_excludes_upwork_even_when_registered(monkeypatch) -> None:
    """REGISTRY has no `upwork` entry until Task 6, so a bare `"upwork" not in
    adapters_for(["ats"])` assertion can never fail today regardless of whether
    the exclusion exists. Fake a registered upwork adapter so the exclusion in
    `_ats_sources` is actually exercised now, not just after Task 6 lands."""
    from jobhunt import sources as source_registry

    fake_registry = dict(source_registry.REGISTRY)
    fake_registry["upwork"] = object
    monkeypatch.setattr(source_registry, "REGISTRY", fake_registry)

    expanded = preferences._ats_sources()
    assert "upwork" not in expanded
    assert "greenhouse" in expanded and "workable" in expanded


def test_filters_carry_the_selected_adapter_ids() -> None:
    prefs = preferences.Preferences(titles=["Backend Engineer"], sources=["linkedin"])
    assert preferences.to_filters(prefs)["global"]["sources"] == ["linkedin"]


def test_filters_omit_sources_when_everything_is_selected() -> None:
    prefs = preferences.Preferences(titles=["Backend Engineer"], sources=["ats", "linkedin"])
    assert "sources" not in preferences.to_filters(prefs)["global"]


def test_widening_the_source_selection_removes_the_restriction_from_the_document(cfg) -> None:
    """`update` cannot remove a key, so a narrowed selection used to be permanent:
    the fetch path resumed fetching ATS while the shortlist kept dropping it."""
    prefs, _ = preferences.load(cfg)
    prefs.titles = ["Backend Engineer"]
    prefs.sources = ["linkedin"]
    preferences.save(cfg, prefs)
    _, narrowed = preferences.load(cfg)
    assert narrowed["global"]["sources"] == ["linkedin"]

    prefs.sources = ["ats", "linkedin"]
    preferences.save(cfg, prefs)
    _, widened = preferences.load(cfg)
    assert "sources" not in widened["global"]


def test_clearing_every_source_is_rejected(cfg) -> None:
    with pytest.raises(preferences.PreferenceError):
        set_prefs(cfg, sources="none")


def test_upwork_alone_is_now_a_valid_selection(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(prefs, {"sources": "upwork"})
    assert updated.sources == ["upwork"]


# --- upwork: its own query settings --------------------------------------------


def test_upwork_preferences_default_to_both_job_types() -> None:
    assert preferences.Preferences().upwork.job_types == ["hourly", "fixed"]


def test_a_dotted_upwork_key_is_applied(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(
        prefs, {"upwork.min_hourly": "35", "upwork.queries": "rag, fastapi"}
    )
    assert updated.upwork.min_hourly == 35.0
    assert updated.upwork.queries == ["rag", "fastapi"]


def test_an_unknown_upwork_key_is_a_loud_error(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    with pytest.raises(preferences.PreferenceError):
        preferences.apply_updates(prefs, {"upwork.nonsense": "1"})


def test_an_unknown_experience_level_is_refused(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    with pytest.raises(preferences.PreferenceError):
        preferences.apply_updates(prefs, {"upwork.experience_level": "advanced"})


def test_upwork_settings_survive_a_save_and_load(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs.upwork.min_hourly = 40.0
    prefs.upwork.queries = ["llm"]
    preferences.save(cfg, prefs)
    loaded, _ = preferences.load(cfg)
    assert isinstance(loaded.upwork, preferences.UpworkPreferences)
    assert loaded.upwork.min_hourly == 40.0 and loaded.upwork.queries == ["llm"]


def test_upwork_preferences_round_trip_through_from_filters() -> None:
    """Task 1's `_coerce` threads the resolved type hint through automatically -
    nothing in this module needs to special-case the nested dataclass."""
    prefs = preferences.Preferences(upwork=preferences.UpworkPreferences(min_hourly=50.0))
    filters = preferences.to_filters(prefs)
    filters[preferences.MANAGED_KEY] = dataclasses.asdict(prefs)
    restored = preferences.from_filters(filters)
    assert isinstance(restored.upwork, preferences.UpworkPreferences)
    assert restored.upwork.min_hourly == 50.0


def test_the_client_key_is_omitted_when_both_switches_are_off() -> None:
    """Same discipline as `sources`: an always-present key churns the filter
    fingerprint and re-gates the whole corpus on every write."""
    prefs = preferences.Preferences()
    assert "client" not in preferences.to_filters(prefs)["profiles"]["upwork"]


def test_the_client_rules_reach_the_upwork_profile() -> None:
    prefs = preferences.Preferences(
        upwork=preferences.UpworkPreferences(require_verified_client=True, client_min_spend=500.0)
    )
    client = preferences.to_filters(prefs)["profiles"]["upwork"]["client"]
    assert client == {"require_verified": True, "min_spend": 500.0}


def test_a_spend_floor_removed_again_leaves_no_rule_behind(cfg) -> None:
    """`update` adds keys and never removes them, so without `client` in the
    owned list a floor could be set but never cleared."""
    prefs, _ = preferences.load(cfg)
    prefs.upwork.client_min_spend = 250.0
    preferences.save(cfg, prefs)

    prefs, _ = preferences.load(cfg)
    assert prefs.upwork.client_min_spend == 250.0
    prefs.upwork.client_min_spend = None
    preferences.save(cfg, prefs)

    document = yaml.safe_load(preferences.filters_path(cfg).read_text(encoding="utf-8"))
    assert "client" not in document["profiles"]["upwork"]


def test_the_old_spend_switch_becomes_a_one_dollar_floor() -> None:
    """`require_client_spend: true` meant "has spent something": $1 says the same."""
    restored = preferences.from_filters(
        {preferences.MANAGED_KEY: {"upwork": {"require_client_spend": True}}}
    )
    assert restored.upwork.client_min_spend == 1.0


def test_the_old_spend_switch_left_off_sets_no_floor() -> None:
    restored = preferences.from_filters(
        {preferences.MANAGED_KEY: {"upwork": {"require_client_spend": False}}}
    )
    assert restored.upwork.client_min_spend is None


def test_a_new_spend_floor_is_never_overwritten_by_the_old_switch() -> None:
    restored = preferences.from_filters(
        {preferences.MANAGED_KEY: {"upwork": {"require_client_spend": True, "client_min_spend": 250.0}}}
    )
    assert restored.upwork.client_min_spend == 250.0


def test_the_new_client_settings_apply_from_text(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(prefs, {
        "upwork.client_locations": "United States, Canada",
        "upwork.client_min_hires": "1",
        "upwork.client_max_hires": "9",
        "upwork.proposals_max": "20",
        "upwork.client_min_spend": "$1,000",
        "upwork.recommended_feed": "true",
    })
    assert updated.upwork.client_locations == ["United States", "Canada"]
    assert updated.upwork.client_min_hires == 1
    assert updated.upwork.client_max_hires == 9
    assert updated.upwork.proposals_max == 20
    assert updated.upwork.client_min_spend == 1000.0
    assert updated.upwork.recommended_feed is True


def test_a_location_typed_twice_in_another_case_is_kept_once(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(
        prefs, {"upwork.client_locations": "United States,  united states , Canada"}
    )
    assert updated.upwork.client_locations == ["United States", "Canada"]


def test_a_zero_spend_floor_means_no_rule(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(prefs, {"upwork.client_min_spend": "0"})
    assert updated.upwork.client_min_spend is None


def test_a_negative_count_is_refused(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    with pytest.raises(preferences.PreferenceError, match="upwork.proposals_max"):
        preferences.apply_updates(prefs, {"upwork.proposals_max": "-1"})


def test_hires_min_above_max_is_refused_against_the_max(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    with pytest.raises(preferences.PreferenceError) as excinfo:
        preferences.apply_updates(
            prefs, {"upwork.client_min_hires": "10", "upwork.client_max_hires": "2"}
        )
    assert str(excinfo.value).startswith("upwork.client_max_hires:")


def test_hires_of_zero_to_zero_is_allowed(cfg) -> None:
    """Upwork's own example for "clients with no hires yet"."""
    prefs, _ = preferences.load(cfg)
    updated = preferences.apply_updates(
        prefs, {"upwork.client_min_hires": "0", "upwork.client_max_hires": "0"}
    )
    assert (updated.upwork.client_min_hires, updated.upwork.client_max_hires) == (0, 0)


def test_the_feed_query_is_reserved(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    with pytest.raises(preferences.PreferenceError, match="upwork.queries"):
        preferences.apply_updates(prefs, {"upwork.queries": "rag, @feed"})


def test_the_new_client_settings_survive_a_save_and_load(cfg) -> None:
    prefs, _ = preferences.load(cfg)
    prefs.upwork.client_locations = ["United States"]
    prefs.upwork.client_max_hires = 9
    prefs.upwork.recommended_feed = True
    preferences.save(cfg, prefs)
    loaded, _ = preferences.load(cfg)
    assert loaded.upwork.client_locations == ["United States"]
    assert loaded.upwork.client_max_hires == 9
    assert loaded.upwork.recommended_feed is True


def test_a_market_profile_this_writer_creates_is_seeded_from_the_packaged_one(cfg) -> None:
    """The real machine's filters.yaml predates Upwork: it has yc, global_remote
    and tr_local and no upwork block. `install_user_copies` never overwrites an
    existing file, and `save` only writes the keys it owns, so the created
    profile used to have no `llm_gate_prompt` and no `min_score_to_surface` -
    the Upwork batch was gated with an empty prompt and no surfacing bar."""
    path = preferences.filters_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"profiles": {"global_remote": {"llm_gate_prompt": "prompts/mine.md"}}}),
        encoding="utf-8",
    )

    prefs, _ = preferences.load(cfg)
    preferences.save(cfg, prefs)

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    upwork = document["profiles"]["upwork"]
    assert upwork["llm_gate_prompt"] == "prompts/gate_upwork.md"
    assert upwork["min_score_to_surface"] == 0.6
    # A profile the user already had keeps whatever they put in it.
    assert document["profiles"]["global_remote"]["llm_gate_prompt"] == "prompts/mine.md"


def test_seeding_never_touches_a_profile_that_already_exists(cfg) -> None:
    path = preferences.filters_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"profiles": {"upwork": {"min_score_to_surface": 0.9}}}), encoding="utf-8"
    )

    prefs, _ = preferences.load(cfg)
    preferences.save(cfg, prefs)

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["profiles"]["upwork"]["min_score_to_surface"] == 0.9
    assert "llm_gate_prompt" not in document["profiles"]["upwork"]
