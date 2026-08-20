"""The HTTP surface.

The browser is a convenience, never the gate: every rule the form enforces is
enforced again here, because a filter that reaches the scraper broken wastes a
whole run.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt.web.app import create_app


@pytest.fixture
def client(cfg):
    return TestClient(create_app(config=cfg))


def test_filters_come_back_with_the_vocabularies_the_form_needs(client):
    body = client.get("/api/filters").json()

    assert "filters" in body
    assert body["vocab"]["titles"], "the titles field needs something to suggest"
    assert body["vocab"]["locations"], "the locations field needs something to suggest"


def test_every_location_suggestion_carries_a_count(client):
    locations = client.get("/api/filters").json()["vocab"]["locations"]

    assert all("count" in row and "label" in row for row in locations)


def test_saving_filters_returns_what_was_stored(client):
    body = client.post("/api/filters", json={"titles": ["AI Engineer"], "top_n": 25}).json()

    assert body["filters"]["titles"] == ["AI Engineer"]
    assert body["filters"]["top_n"] == 25


def test_a_ceiling_below_the_floor_is_refused_with_the_field_named(client):
    response = client.post(
        "/api/filters", json={"experience_min": "senior", "experience_max": "mid"}
    )

    assert response.status_code == 422
    assert response.json()["field"] == "experience_max"


def test_a_location_that_cannot_be_placed_is_refused(client):
    response = client.post("/api/filters", json={"locations": ["Atlantis"]})

    assert response.status_code == 422
    assert "Atlantis" in response.json()["message"]


def test_a_salary_of_letters_is_refused(client):
    response = client.post("/api/filters", json={"min_salary": "quite a lot"})

    assert response.status_code == 422
    assert response.json()["field"] == "min_salary"


def test_filters_survive_the_round_trip(client):
    client.post(
        "/api/filters",
        json={"titles": ["Platform Engineer"], "max_age": {"value": 2, "unit": "weeks"}},
    )

    body = client.get("/api/filters").json()

    assert body["filters"]["titles"] == ["Platform Engineer"]
    assert body["filters"]["max_age_days"] == 14


def test_there_is_no_run_before_one_is_started(client):
    body = client.get("/api/runs/current").json()

    assert body["phase"] == "idle"
    assert body["running"] is False


def test_stopping_when_nothing_runs_is_not_an_error(client):
    response = client.post("/api/runs/current/stop")

    assert response.status_code == 200
    assert response.json()["stop_requested"] is False


def test_the_app_serves_the_ui_route(client):
    """The bundle may not be built yet, but the route must exist."""
    response = client.get("/")

    assert response.status_code in (200, 503)


def test_starting_a_run_reports_it_as_running(client, monkeypatch):
    from jobhunt.web import app as app_module

    monkeypatch.setattr(app_module, "build_pipeline", lambda cfg, hooks: _SlowPipeline())

    started = client.post("/api/runs").json()

    assert started["started"] is True


def test_a_second_start_while_one_runs_is_refused(client, monkeypatch):
    from jobhunt.web import app as app_module

    monkeypatch.setattr(app_module, "build_pipeline", lambda cfg, hooks: _SlowPipeline())
    client.post("/api/runs")

    response = client.post("/api/runs")

    assert response.status_code == 409


def test_a_run_cannot_start_while_the_filters_are_broken(client, monkeypatch):
    """A broken filter set would waste the whole run, so it never reaches the engine."""
    from jobhunt.web import app as app_module

    monkeypatch.setattr(app_module, "build_pipeline", lambda cfg, hooks: _SlowPipeline())
    client.post("/api/filters", json={"experience_min": "junior", "experience_max": "mid"})

    assert client.post("/api/runs").json()["started"] is True


class _SlowPipeline:
    """Enough of the Protocol to start, slow enough to still be running."""

    sources = ["greenhouse"]

    def boards_for(self, source):
        return list(range(50))

    def fetch_board(self, source, board):
        import time

        time.sleep(0.05)
        return 1

    def rank(self):
        return 0

    def gate_batches(self):
        return []

    def gate(self, batch):
        return None

    def shortlist(self):
        return []


# --- profile ---------------------------------------------------------------


def _with_master(cfg, tmp_path):
    path = tmp_path / "CV_Source" / "master.tex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n", encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(path)
    return path


def test_the_master_cv_can_be_read(cfg, tmp_path):
    _with_master(cfg, tmp_path)
    client = TestClient(create_app(config=cfg))

    body = client.get("/api/profile").json()

    assert "documentclass" in body["text"]


def test_saving_the_master_takes_a_backup(cfg, tmp_path):
    _with_master(cfg, tmp_path)
    client = TestClient(create_app(config=cfg))

    client.post("/api/profile", json={"text": "\\documentclass{article}\\begin{document}y\\end{document}"})

    assert client.get("/api/profile/backups").json()["backups"]


def test_an_empty_master_is_refused_by_the_server(cfg, tmp_path):
    _with_master(cfg, tmp_path)
    client = TestClient(create_app(config=cfg))

    response = client.post("/api/profile", json={"text": "  "})

    assert response.status_code == 422


def test_restoring_a_path_outside_the_backup_folder_is_refused(cfg, tmp_path):
    _with_master(cfg, tmp_path)
    client = TestClient(create_app(config=cfg))

    response = client.post("/api/profile/restore", json={"name": "../../etc/passwd"})

    assert response.status_code == 422


# --- tailoring -------------------------------------------------------------


def test_tailoring_nothing_is_refused(client):
    response = client.post("/api/tailor", json={"job_ids": []})

    assert response.status_code == 422


def test_the_tailoring_state_starts_empty(client):
    body = client.get("/api/tailor").json()

    assert body["jobs"] == []
    assert body["running"] is False
