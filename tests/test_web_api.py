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

    def sources(self):
        return ["greenhouse"]

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


# --- tailoring -------------------------------------------------------------


def test_tailoring_nothing_is_refused(client):
    response = client.post("/api/tailor", json={"job_ids": []})

    assert response.status_code == 422


def test_the_tailoring_state_starts_empty(client):
    body = client.get("/api/tailor").json()

    assert body["jobs"] == []
    assert body["running"] is False


def test_a_gate_batch_payload_never_reaches_the_browser(cfg):
    """The payload is the prompt plus every job description in the batch. The
    page polls this endpoint every two seconds and never reads it."""
    from jobhunt.web import events as events_module
    from jobhunt.web import runs as runs_module
    from jobhunt.web.app import AppState, create_app

    app = create_app(config=cfg)
    state: AppState = app.state.jh
    supervisor = runs_module.RunSupervisor(pipeline=None, log=events_module.EventLog())
    supervisor.state.gate = runs_module.GateReport(
        plan=runs_module.GatePlan(
            batches=[
                runs_module.GateBatch(
                    label="eu", size=2, payload={"prompt": "x" * 40000, "jobs": [{}, {}]}
                )
            ],
            jobs=2,
        )
    )
    state.supervisor = supervisor

    body = TestClient(app).get("/api/runs/current").json()

    batch = body["gate"]["plan"]["batches"][0]
    assert batch == {"label": "eu", "size": 2, "status": "queued"}


def test_a_run_that_has_not_started_reports_no_rank_or_gate(client):
    body = client.get("/api/runs/current").json()

    assert body["rank"] is None
    assert body["gate"] is None


# --- the revision studio --------------------------------------------------------


@pytest.fixture
def studio(cfg, tmp_path):
    """An app whose batch has already shipped one CV, with a fake agent and a
    fake toolchain so no model and no LaTeX are needed."""
    from jobhunt.web import revise as revise_module
    from jobhunt.web import tailor as tailor_module
    from jobhunt.web.app import create_app
    from tests.test_web_revise import TEX, FakeAgent, FakeLatex

    folder = tmp_path / "Tailored CVs" / "Arc Bank - Backend Engineer"
    folder.mkdir(parents=True)
    (folder / "cv.tex").write_text(TEX, encoding="utf-8")

    app = create_app(config=cfg)
    state = app.state.jh
    state.desk = revise_module.ReviseDesk(cfg, agent=FakeAgent(), latex=FakeLatex())
    batch = tailor_module.TailorBatch(job_ids=[7], steps=None, log=state.log)
    batch.rows[0].folder = str(folder)
    batch.rows[0].state = "approved"
    batch.rows[0].title = "Backend Engineer"
    batch.rows[0].company = "Arc Bank"
    state.batch = batch
    return TestClient(app), state, folder


def _settle(client, job_id=7):
    import time

    for _ in range(200):
        body = client.get(f"/api/revise/{job_id}").json()
        if not body.get("thinking"):
            return body
        time.sleep(0.01)
    raise AssertionError("the revision never finished")


def test_only_jobs_with_a_folder_can_be_revised(studio):
    client, state, _ = studio
    state.batch.rows[0].folder = None

    assert client.get("/api/revise").json()["jobs"] == []


def test_opening_a_revision_returns_the_thread_and_the_job_it_belongs_to(studio):
    client, _, _ = studio

    body = client.post("/api/revise/7").json()

    assert body["company"] == "Arc Bank"
    assert body["turns"][0]["role"] == "agent"
    assert body["ahead"] == 0


def test_a_job_that_was_never_tailored_is_refused_in_words(studio):
    client, _, _ = studio

    response = client.post("/api/revise/999")

    assert response.status_code == 422
    assert "no tailored CV" in response.json()["message"]


def test_a_message_comes_back_with_the_change_the_agent_made(studio):
    client, _, _ = studio
    client.post("/api/revise/7")

    client.post("/api/revise/7/message", json={"message": "lead with the LLM work"})
    body = _settle(client)

    assert [turn["role"] for turn in body["turns"]][-2:] == ["you", "agent"]
    assert body["turns"][-1]["changes"]
    assert body["ahead"] == 1


def test_the_preview_is_a_pdf_the_browser_can_render(studio):
    client, _, _ = studio
    client.post("/api/revise/7")

    body = client.get("/api/revise/7/preview").json()

    assert body["ok"] and body["pdf"]
    assert body["pages"] == 2


def test_the_folder_is_only_written_when_sync_is_called(studio):
    client, _, folder = studio
    from tests.test_web_revise import TEX

    client.post("/api/revise/7")
    client.post("/api/revise/7/message", json={"message": "change something"})
    _settle(client)
    assert (folder / "cv.tex").read_text() == TEX, "untouched until sync"

    body = client.post("/api/revise/7/sync").json()

    assert body["ahead"] == 0
    assert (folder / "cv.pdf").exists()


def test_syncing_with_nothing_to_write_is_refused_rather_than_silent(studio):
    client, _, _ = studio
    client.post("/api/revise/7")

    response = client.post("/api/revise/7/sync")

    assert response.status_code == 422
    assert "nothing to sync" in response.json()["message"]


# --- results that outlive the process --------------------------------------------


def _a_shortlisted_job(cfg) -> int:
    """One job in the corpus that the gate scored above the bar."""
    from jobhunt import store
    from jobhunt.db.models import Score
    from jobhunt.db.session import session_scope
    from jobhunt.sources.base import JobPosting

    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(
            source="ashby", external_id="x1", market="global_remote",
            title="AI Engineer", company_name="Apify", remote_type="hybrid",
            description_text="We build things. " * 40,
            apply_url="https://jobs.ashbyhq.com/apify/1",
        ))
        session.flush()
        session.add(Score(
            job_id=job.id, profile=job.market, deterministic_pass=True,
            deterministic_notes={"passed": True, "boost": 1.0},
            llm_score=0.85, llm_reasoning="close match", llm_model="claude-code-print",
        ))
        return job.id


def test_with_no_run_in_memory_the_shortlist_is_read_back_from_the_corpus(cfg):
    """Restarting the server must not lose work that is already on disk."""
    from jobhunt.web.app import create_app

    kept = _a_shortlisted_job(cfg)

    body = TestClient(create_app(config=cfg)).get("/api/runs/current").json()

    assert body["outcome"] == "restored"
    assert body["running"] is False
    assert [row["job_id"] for row in body["results"] if not row["below_bar"]] == [kept]
    assert body["rank"] is None and body["gate"] is None, "nothing was watched happening here"


def test_an_empty_corpus_restores_nothing_and_says_nothing(client):
    body = client.get("/api/runs/current").json()

    assert body["outcome"] is None
    assert body["results"] == []


def test_a_job_still_being_tailored_is_listed_but_not_ready(studio):
    """The studio has to tell "no CV yet" apart from "CV ready", or it offers a
    job it cannot open and looks broken."""
    client, state, folder = studio
    (folder / "cv.tex").unlink()

    row = client.get("/api/revise").json()["jobs"][0]

    assert row["folder"], "the folder exists from the moment tailoring starts"
    assert row["ready"] is False


def test_a_job_whose_cv_has_landed_is_ready(studio):
    client, _, _ = studio

    assert client.get("/api/revise").json()["jobs"][0]["ready"] is True


def test_a_job_whose_cv_is_still_being_written_is_not_ready(studio):
    """A folder can hold a cv.tex that the agent has not finished writing."""
    client, state, folder = studio
    (folder / "cv.tex").write_text("\\documentclass{article}\n", encoding="utf-8")

    assert client.get("/api/revise").json()["jobs"][0]["ready"] is False


def test_a_job_the_batch_is_still_working_on_is_not_ready(studio):
    """Round two overwrites the CV round one produced, so a complete file is
    not on its own a safe one to copy."""
    client, state, _ = studio
    state.batch.rows[0].state = "running"

    assert client.get("/api/revise").json()["jobs"][0]["ready"] is False


# --- run history and the two buttons ---------------------------------------------


def test_no_runs_on_file_is_an_empty_list(client):
    assert client.get("/api/runs").json() == {"runs": []}


def test_a_saved_run_is_listed_and_can_be_opened(cfg):
    from jobhunt.web import history as history_module
    from jobhunt.web.app import create_app

    history_module.save(cfg, "20260822-100000", {
        "phase": "done", "outcome": "completed", "started_at": "s", "finished_at": "f",
        "counters": {"jobs_total": 4213},
        "results": [{"below_bar": False}, {"below_bar": True}],
        "rank": {"passed": 412}, "gate": None,
    })
    client = TestClient(create_app(config=cfg))

    listed = client.get("/api/runs").json()["runs"]
    assert [row["run_id"] for row in listed] == ["20260822-100000"]
    assert listed[0]["shortlisted"] == 1

    body = client.get("/api/runs/20260822-100000").json()
    assert body["rank"]["passed"] == 412, "the whole state comes back, not a summary"


def test_a_run_that_was_pruned_is_a_404_not_a_crash(client):
    assert client.get("/api/runs/20200101-000000").status_code == 404


def test_pausing_nothing_says_so_rather_than_failing(client):
    body = client.post("/api/runs/current/pause").json()

    assert body == {"paused": False, "reason": "nothing is running"}


def test_resuming_with_no_paused_run_is_refused(client):
    response = client.post("/api/runs/current/resume")

    assert response.status_code == 409
    assert "no paused run" in response.json()["message"]


def test_a_request_touches_the_idle_clock(client) -> None:
    """The watchdog measures requests, so every route has to count as one."""
    state = client.app.state.jh
    state.idle._last -= 500.0
    before = state.idle.idle_for()
    client.get("/api/applied")
    assert state.idle.idle_for() < before


def test_a_running_batch_counts_as_busy(client) -> None:
    state = client.app.state.jh
    assert not state.busy()

    class Batch:
        running = True

    state.batch = Batch()
    assert state.busy()


# --- a finished run goes stale ------------------------------------------------


def test_a_finished_run_reflects_scores_written_after_it(cfg, client) -> None:
    """The shortlist a finished run left behind is a snapshot, and the corpus
    moves under it: a rescore, a gate ingest or an edit to the filters all
    change what should be on screen. Reloading the page has to show that,
    rather than serving the same frozen list until the process restarts.
    """
    from jobhunt.db.models import Job, Score
    from jobhunt.db.session import session_scope

    state = client.app.state.jh

    class FinishedRun:
        phase, running, outcome, error = "done", False, "completed", None
        degraded, counters, results = [], {}, []
        run_id, started_at, finished_at = "r1", "t0", "t1"
        resumable, rank, gate = False, None, None

    class FinishedSupervisor:
        pausing = stopping = False
        state = FinishedRun()

    state.supervisor = FinishedSupervisor()
    assert client.get("/api/runs/current").json()["results"] == []

    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="late-1", source="ashby", market="global_remote",
            title="AI Engineer", title_normalized="ai engineer", is_active=True,
        )
        session.add(job)
        session.flush()
        session.add(
            Score(
                job_id=job.id, profile="global_remote", deterministic_pass=True,
                deterministic_notes={"passed": True, "reasons": [], "codes": []},
                llm_score=0.9, llm_reasoning="fits",
            )
        )

    titles = [row["title"] for row in client.get("/api/runs/current").json()["results"]]
    assert "AI Engineer" in titles


# --- pages ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/shortlist",
        "/tailoring",
        "/tailoring/79273",
        "/runs/20260923-170200",
        "/search",
        "/cv",
        "/cv/templates",
        "/filters",
        "/profile",
        "/templates",
    ],
)
def test_every_page_the_interface_routes_is_served(client, path):
    """A refresh or a pasted link on any page must load the app, not a 404."""
    assert client.get(path).status_code in (200, 503)


@pytest.mark.parametrize("path", ["/nowhere", "/tailoring/abc", "/cv/nothing", "/runs/a/b"])
def test_a_mistyped_path_still_says_it_does_not_exist(client, path):
    assert client.get(path).status_code == 404


# --- saved runs: rename and delete -------------------------------------------------


def _saved_run(cfg, run_id="20260822-100000"):
    from jobhunt.web import history as history_module

    body = {"run_id": run_id, "phase": "done", "outcome": "completed", "results": []}
    history_module.save(cfg, run_id, body)
    return run_id


def test_a_saved_run_can_be_renamed(client, cfg):
    run_id = _saved_run(cfg)

    response = client.patch(f"/api/runs/{run_id}", json={"name": "Berlin push"})

    assert response.status_code == 200
    assert client.get("/api/runs").json()["runs"][0]["name"] == "Berlin push"


def test_renaming_a_run_not_on_file_is_a_404(client):
    response = client.patch("/api/runs/20260822-100000", json={"name": "Berlin push"})

    assert response.status_code == 404


def test_an_overlong_name_is_refused(client, cfg):
    from jobhunt.web import history as history_module

    run_id = _saved_run(cfg)

    response = client.patch(f"/api/runs/{run_id}", json={"name": "x" * (history_module.NAME_MAX + 1)})

    assert response.status_code == 422


def test_a_saved_run_can_be_deleted(client, cfg):
    run_id = _saved_run(cfg)

    response = client.delete(f"/api/runs/{run_id}")

    assert response.status_code == 200
    assert client.get("/api/runs").json()["runs"] == []
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_deleting_a_run_not_on_file_is_a_404(client):
    assert client.delete("/api/runs/20260822-100000").status_code == 404


@pytest.mark.parametrize("phase", ["sync", "paused"])
def test_the_run_in_progress_or_paused_cannot_be_deleted(cfg, phase):
    from jobhunt.web import events as events_module
    from jobhunt.web import runs as runs_module

    run_id = _saved_run(cfg)
    app = create_app(config=cfg)
    supervisor = runs_module.RunSupervisor(pipeline=None, log=events_module.EventLog())
    supervisor.state.run_id = run_id
    supervisor.state.phase = phase
    app.state.jh.supervisor = supervisor

    response = TestClient(app).delete(f"/api/runs/{run_id}")

    assert response.status_code == 409
    assert TestClient(app).get(f"/api/runs/{run_id}").status_code == 200


# --- clearing the live shortlist ---------------------------------------------------


def test_a_cleared_live_shortlist_stays_empty_until_the_next_run(cfg):
    _a_shortlisted_job(cfg)
    client = TestClient(create_app(config=cfg))

    response = client.post("/api/runs/current/clear")

    assert response.status_code == 200
    body = client.get("/api/runs/current").json()
    assert body["results"] == []
    # The Run page still reads as idle after a run, not as "no run yet".
    assert body["outcome"] == "restored"


def test_a_cleared_live_shortlist_survives_a_restart(cfg):
    _a_shortlisted_job(cfg)
    TestClient(create_app(config=cfg)).post("/api/runs/current/clear")

    body = TestClient(create_app(config=cfg)).get("/api/runs/current").json()

    assert body["results"] == []


def test_starting_a_run_brings_the_live_shortlist_back(cfg, monkeypatch):
    from jobhunt.web import app as app_module
    from jobhunt.web import history as history_module

    _a_shortlisted_job(cfg)
    monkeypatch.setattr(app_module, "build_pipeline", lambda cfg, hooks: _SlowPipeline())
    client = TestClient(create_app(config=cfg))
    client.post("/api/runs/current/clear")

    client.post("/api/runs")

    assert history_module.live_cleared(cfg) is False


def test_clearing_while_a_run_is_going_is_refused(client, monkeypatch):
    from jobhunt.web import app as app_module

    monkeypatch.setattr(app_module, "build_pipeline", lambda cfg, hooks: _SlowPipeline())
    client.post("/api/runs")

    assert client.post("/api/runs/current/clear").status_code == 409


def test_the_filters_carry_the_authorization_settings(client):
    body = client.get("/api/filters").json()
    assert body["filters"]["work_authorization"] == []
    assert body["filters"]["sponsorship_required"] is False
