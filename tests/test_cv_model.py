"""The profile as data.

Validation happens here, once, for every way a profile arrives: the form, a
restored backup, and the import. Each refusal names the field it is about, as a
dotted path the form can put next to the right input.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_fixture

from jobhunt.cv import model


def fixture() -> dict:
    return copy.deepcopy(load_fixture("cv/profile.json"))


def minimal() -> dict:
    return {"basics": {"name": "Ada Lovelace"}}


def problem(data) -> tuple[str, str]:
    with pytest.raises(model.ProfileInvalid) as caught:
        model.parse(data)
    return caught.value.field, caught.value.message


def test_a_minimal_profile_gets_the_default_layout():
    profile = model.parse(minimal())

    assert [ref.key for ref in profile.layout] == list(model.FIXED_SECTIONS)
    assert profile.layout[0].title == "PROFESSIONAL SUMMARY"
    assert profile.schema_version == model.SCHEMA_VERSION


def test_the_fixture_round_trips_unchanged():
    profile = model.parse(fixture())

    assert model.parse(profile.model_dump(mode="json")) == profile


def test_reordering_keeps_every_id():
    data = fixture()
    data["experience"].reverse()

    assert [entry.id for entry in model.parse(data).experience] == ["e2", "e1"]


def test_dates_join_what_is_there():
    profile = model.parse(fixture())

    assert profile.experience[0].dates == "April 2026 - Present"
    assert profile.experience[1].dates == "2019"


def test_a_missing_name_is_refused_at_its_field():
    assert problem({"basics": {}}) == ("basics.name", "is required")


def test_an_empty_bullet_is_refused_at_its_field():
    data = fixture()
    data["experience"][0]["bullets"][0]["text"] = "   "

    assert problem(data) == ("experience.0.bullets.0.text", "cannot be empty")


def test_an_entry_that_says_nothing_is_refused():
    data = minimal() | {"experience": [{"id": "e1"}]}

    assert problem(data) == ("experience.0", "needs a title or at least one bullet")


def test_a_link_that_is_not_web_or_mail_is_refused():
    data = fixture()
    data["basics"]["links"][0]["url"] = "javascript:alert(1)"

    field, message = problem(data)
    assert field == "basics.links.0.url"
    assert "http" in message


def test_a_duplicate_id_is_refused_where_it_repeats():
    data = fixture()
    data["projects"][0]["id"] = "e1"

    field, message = problem(data)
    assert field == "projects.0.id"
    assert "used twice" in message


def test_a_layout_key_for_a_section_that_does_not_exist_is_refused():
    data = minimal() | {"layout": [{"key": "hobbies", "title": "HOBBIES"}]}

    field, message = problem(data)
    assert field == "layout.0.key"
    assert "hobbies" in message


def test_an_unknown_field_is_refused():
    data = minimal() | {"photo": "me.jpg"}

    assert problem(data) == ("photo", "is not a field a profile has")


def test_a_profile_from_a_newer_version_is_refused():
    data = minimal() | {"schema_version": 2}

    field, message = problem(data)
    assert field == "schema_version"
    assert "newer" in message


def test_every_problem_is_reported_not_just_the_first():
    data = {"basics": {"name": "", "email": "nope"}}

    with pytest.raises(model.ProfileInvalid) as caught:
        model.parse(data)
    fields = [field for field, _ in caught.value.problems]
    assert fields == ["basics.name", "basics.email"]


def test_the_fingerprint_ignores_key_order_and_sees_content():
    data = fixture()
    shuffled = dict(reversed(list(data.items())))
    changed = fixture()
    changed["experience"][0]["bullets"][0]["text"] = "Something else."

    assert model.parse(data).fingerprint() == model.parse(shuffled).fingerprint()
    assert model.parse(data).fingerprint() != model.parse(changed).fingerprint()
