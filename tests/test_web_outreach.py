"""Outreach over HTTP.

Refusals are the interesting half: a cap, an illegal transition, and a body too
long all have to arrive as a stated reason the drawer can show, not a 500.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt.db.models import Job
from jobhunt.db.session import session_scope
from jobhunt.outreach import provider
from jobhunt.web.app import create_app


@pytest.fixture
def client(cfg):
    return TestClient(create_app(config=cfg))


@pytest.fixture
def job_id(cfg) -> int:
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="ext-1", source="greenhouse", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
        )
        session.add(job)
        session.flush()
        return job.id


def add(client, job_id, name="Deniz Aksoy", url=None):
    return client.post(
        f"/api/outreach/{job_id}/contacts", json={"full_name": name, "profile_url": url}
    )


def test_an_empty_job_has_no_contacts_and_a_budget(client, job_id):
    body = client.get(f"/api/outreach/{job_id}").json()
    assert body["contacts"] == []
    assert body["budget"]["invites_max"] == 20


def test_adding_a_contact_then_reading_it_back(client, job_id):
    assert add(client, job_id).status_code == 200
    body = client.get(f"/api/outreach/{job_id}").json()
    assert body["contacts"][0]["full_name"] == "Deniz Aksoy"
    assert body["contacts"][0]["needs_choice"] is True


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/outreach/9999").status_code == 404


def test_status_then_draft_then_approve(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": True})
    drafted = client.post(f"/api/outreach/{job_id}/{contact_id}/draft").json()
    assert drafted["route"] == provider.DM
    sent = client.post(f"/api/outreach/{job_id}/{contact_id}/approve", json={}).json()
    assert sent["state"] == "sent"


def test_approving_twice_is_a_409(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{job_id}/{contact_id}/draft")
    client.post(f"/api/outreach/{job_id}/{contact_id}/approve", json={})
    again = client.post(f"/api/outreach/{job_id}/{contact_id}/approve", json={})
    assert again.status_code == 409


def test_a_cap_is_a_409_that_names_the_budget(client, cfg, job_id):
    cfg.raw["outreach"]["max_daily_dms"] = 0
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{job_id}/{contact_id}/draft")
    refused = client.post(f"/api/outreach/{job_id}/{contact_id}/approve", json={})
    assert refused.status_code == 409
    assert "cap" in refused.json()["detail"].lower()


def test_an_over_long_invite_note_is_a_422(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(
        f"/api/outreach/contacts/{contact_id}/status",
        json={"is_connection": False, "can_send_inmail": False},
    )
    client.post(
        f"/api/outreach/{job_id}/{contact_id}/draft", json={"route": provider.INVITE_NOTE}
    )
    client.put(f"/api/outreach/{job_id}/{contact_id}/body", json={"body": "x" * 400})
    refused = client.post(
        f"/api/outreach/{job_id}/{contact_id}/approve", json={"route": provider.INVITE_NOTE}
    )
    assert refused.status_code == 422


def test_the_body_endpoint_keeps_what_was_written(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{job_id}/{contact_id}/draft")
    client.put(f"/api/outreach/{job_id}/{contact_id}/body", json={"body": "mine"})
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": False})
    body = client.get(f"/api/outreach/{job_id}").json()["contacts"][0]
    assert body["body"] == "mine"


def test_the_body_endpoint_with_a_route_returns_the_new_route_and_body(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(
        f"/api/outreach/contacts/{contact_id}/status",
        json={"is_connection": False, "can_send_inmail": False},
    )
    updated = client.put(
        f"/api/outreach/{job_id}/{contact_id}/body",
        json={"body": "switched routes, kept my words", "route": provider.INVITE_NOTE},
    ).json()
    assert updated["route"] == provider.INVITE_NOTE
    assert updated["body"] == "switched routes, kept my words"


def test_the_budget_endpoint_answers_on_its_own(client):
    body = client.get("/api/outreach/budget").json()
    assert body["dms_max"] == 25


def test_states_reports_the_most_advanced_state_per_job(client, cfg):
    with session_scope(cfg.db_path) as session:
        sent_job = Job(
            external_id="ext-sent", source="greenhouse", market="global_remote",
            title="A", title_normalized="a",
        )
        drafted_job = Job(
            external_id="ext-drafted", source="greenhouse", market="global_remote",
            title="B", title_normalized="b",
        )
        untouched_job = Job(
            external_id="ext-none", source="greenhouse", market="global_remote",
            title="C", title_normalized="c",
        )
        session.add_all([sent_job, drafted_job, untouched_job])
        session.flush()
        sent_job_id, drafted_job_id, untouched_job_id = sent_job.id, drafted_job.id, untouched_job.id

    sent_contact = add(client, sent_job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{sent_contact}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{sent_job_id}/{sent_contact}/draft")
    client.post(f"/api/outreach/{sent_job_id}/{sent_contact}/approve", json={})

    drafted_contact = add(client, drafted_job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{drafted_contact}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{drafted_job_id}/{drafted_contact}/draft")

    states = client.get("/api/outreach/states").json()["states"]
    assert states[str(sent_job_id)] == "sent"
    assert states[str(drafted_job_id)] == "drafted"
    assert str(untouched_job_id) not in states
