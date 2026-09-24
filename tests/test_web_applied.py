"""Recording that an application was sent, from the browser.

Marking applied is the only action that takes a job off the shortlist for good,
so it has to be reversible: the reason a tailored job keeps showing up is that
cutting a CV is not the same as sending it, and a mis-click must not undo that.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobhunt.db.models import Application, Job
from jobhunt.db.session import session_scope
from jobhunt.render import csv_export
from jobhunt.web.app import create_app


@pytest.fixture
def client(cfg):
    return TestClient(create_app(config=cfg))


def make_job(cfg, *, status: str | None = None, folder: str | None = None) -> int:
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="ext-1",
            source="wwr",
            market="global_remote",
            title="Software Engineer",
            title_normalized="software engineer",
            is_active=True,
        )
        session.add(job)
        session.flush()
        if status:
            session.add(Application(job_id=job.id, status=status, folder_path=folder))
        return job.id


def status_of(cfg, job_id: int) -> str | None:
    with session_scope(cfg.db_path) as session:
        row = session.scalars(
            select(Application).where(Application.job_id == job_id)
        ).first()
        return row.status if row else None


def test_marking_a_tailored_job_applied_records_it(cfg, client):
    job_id = make_job(cfg, status="tailored", folder="/tmp/cut")

    body = client.post(f"/api/applied/{job_id}", json={"applied": True}).json()

    assert body["applied"] is True
    assert status_of(cfg, job_id) == "applied"


def test_undoing_puts_a_tailored_job_back_where_it_was(cfg, client):
    job_id = make_job(cfg, status="tailored", folder="/tmp/cut")
    client.post(f"/api/applied/{job_id}", json={"applied": True})

    body = client.post(f"/api/applied/{job_id}", json={"applied": False}).json()

    assert body["applied"] is False
    assert status_of(cfg, job_id) == "tailored", "the CV is still cut, only the send is undone"


def test_undoing_a_job_that_was_never_tailored_leaves_no_row_behind(cfg, client):
    job_id = make_job(cfg)
    client.post(f"/api/applied/{job_id}", json={"applied": True})

    client.post(f"/api/applied/{job_id}", json={"applied": False})

    assert status_of(cfg, job_id) is None, "the row existed only to record the send"


def test_marking_twice_is_idempotent(cfg, client):
    job_id = make_job(cfg, status="tailored", folder="/tmp/cut")

    client.post(f"/api/applied/{job_id}", json={"applied": True})
    body = client.post(f"/api/applied/{job_id}", json={"applied": True}).json()

    assert body["applied"] is True
    assert status_of(cfg, job_id) == "applied"


def test_a_job_further_along_is_not_dragged_back(cfg, client):
    job_id = make_job(cfg, status="interview", folder="/tmp/cut")

    client.post(f"/api/applied/{job_id}", json={"applied": True})

    assert status_of(cfg, job_id) == "interview"


def test_an_unknown_job_is_refused_not_crashed(cfg, client):
    response = client.post("/api/applied/999999", json={"applied": True})

    assert response.status_code == 422
    assert "999999" in response.json()["message"]


def test_a_malformed_body_is_refused(cfg, client):
    job_id = make_job(cfg, status="tailored")

    assert client.post(f"/api/applied/{job_id}", json={"applied": "yes"}).status_code == 422


def test_the_csv_column_follows_the_button_both_ways(cfg, client):
    job_id = make_job(cfg, status="tailored", folder="/tmp/cut")
    path = csv_export.csv_path(cfg)
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(
        "job_id,applied,cv_status\n" f"{job_id},FALSE,tailored\n", encoding="utf-8"
    )

    client.post(f"/api/applied/{job_id}", json={"applied": True})
    assert csv_export.read(path)[str(job_id)]["applied"] == csv_export.TRUE

    client.post(f"/api/applied/{job_id}", json={"applied": False})
    assert csv_export.read(path)[str(job_id)]["applied"] != csv_export.TRUE


def test_the_applied_set_is_readable_so_a_view_can_render_the_right_state(cfg, client):
    """Studio and the tailor chat show jobs whose status they do not otherwise
    know. One small read beats threading application state through every
    in-memory session object those screens are built from."""
    tailored = make_job(cfg, status="tailored", folder="/tmp/cut")

    assert client.get("/api/applied").json()["applied"] == []

    client.post(f"/api/applied/{tailored}", json={"applied": True})

    assert client.get("/api/applied").json()["applied"] == [tailored]


def test_undoing_takes_the_job_back_out_of_the_applied_set(cfg, client):
    job_id = make_job(cfg, status="tailored", folder="/tmp/cut")
    client.post(f"/api/applied/{job_id}", json={"applied": True})

    client.post(f"/api/applied/{job_id}", json={"applied": False})

    assert client.get("/api/applied").json()["applied"] == []
