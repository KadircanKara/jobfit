"""Saved title groups, over HTTP.

A group is a named selection of titles the user can pick again after clearing
the field. Saving one is deliberately not saving the filters: naming a set you
might come back to should never change what the next run searches for.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt import preferences as prefs_module
from jobhunt.web.app import create_app


@pytest.fixture
def client(cfg):
    return TestClient(create_app(config=cfg))


def test_a_fresh_install_has_no_groups(client):
    body = client.get("/api/filters").json()

    assert body["filters"]["title_groups"] == {}


def test_a_saved_group_comes_back_with_the_filters(client):
    client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})

    body = client.get("/api/filters").json()

    assert body["filters"]["title_groups"] == {"test": ["backend engineer"]}


def test_saving_a_group_returns_every_group(client):
    client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})
    body = client.post("/api/title-groups", json={"name": "ml", "titles": ["ml engineer"]}).json()

    assert body["title_groups"] == {"test": ["backend engineer"], "ml": ["ml engineer"]}


def test_saving_a_group_does_not_touch_the_titles_in_use(client, cfg):
    client.post("/api/filters", json={"titles": ["data engineer"]})

    saved = client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})

    assert saved.status_code == 200
    prefs, _ = prefs_module.load(cfg)
    assert prefs.titles == ["data engineer"]


def test_a_group_saved_twice_under_one_name_is_replaced(client):
    client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})
    body = client.post("/api/title-groups", json={"name": "test", "titles": ["ml engineer"]}).json()

    assert body["title_groups"] == {"test": ["ml engineer"]}


def test_a_group_without_a_name_is_refused_against_the_titles_field(client):
    response = client.post("/api/title-groups", json={"name": "  ", "titles": ["backend engineer"]})

    assert response.status_code == 422
    assert response.json()["field"] == "titles"


def test_a_group_without_titles_is_refused(client):
    response = client.post("/api/title-groups", json={"name": "test", "titles": []})

    assert response.status_code == 422


def test_deleting_a_group_removes_it(client):
    client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})

    body = client.delete("/api/title-groups/test").json()

    assert body["title_groups"] == {}


def test_deleting_a_group_that_does_not_exist_is_a_404(client):
    response = client.delete("/api/title-groups/nope")

    assert response.status_code == 404
    # The refusal has to name the group, or it reads like a missing endpoint.
    assert "nope" in response.json()["detail"]


def test_deleting_a_group_leaves_the_titles_in_use_alone(client, cfg):
    client.post("/api/filters", json={"titles": ["data engineer"]})
    client.post("/api/title-groups", json={"name": "test", "titles": ["backend engineer"]})

    removed = client.delete("/api/title-groups/test")

    assert removed.status_code == 200
    prefs, _ = prefs_module.load(cfg)
    assert prefs.titles == ["data engineer"]
