"""Outreach over HTTP.

Refusals are the interesting half: a cap, an illegal transition, and a body too
long all have to arrive as a stated reason the drawer can show, not a 500.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt.db.models import Company, Contact, Job
from jobhunt.db.session import session_scope
from jobhunt.outreach import provider
from jobhunt.outreach.unipile_client import UnipileError
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


def test_editing_the_body_of_a_sent_row_is_a_409(client, job_id):
    contact_id = add(client, job_id).json()["contact_id"]
    client.patch(f"/api/outreach/contacts/{contact_id}/status", json={"is_connection": True})
    client.post(f"/api/outreach/{job_id}/{contact_id}/draft")
    client.post(f"/api/outreach/{job_id}/{contact_id}/approve", json={})
    refused = client.put(f"/api/outreach/{job_id}/{contact_id}/body", json={"body": "rewritten"})
    assert refused.status_code == 409
    assert "sent" in refused.json()["detail"]


def test_the_poller_thread_stops_when_the_app_shuts_down(cfg):
    app = create_app(config=cfg)
    with TestClient(app):
        assert not app.state.outreach_stop.is_set()
    assert app.state.outreach_stop.is_set()


class FakeSearchClient:
    def __init__(self, items):
        self.items = items

    def search_people(self, company, keywords, *, limit=5):
        return self.items


class FakeSenderWithClient:
    """Stands in for `UnipileProvider`: a sender whose `.client` can search."""

    def __init__(self, items):
        self.client = FakeSearchClient(items)


class BrokenSearchClient:
    def search_people(self, company, keywords, *, limit=5):
        raise UnipileError("search_people: response body was not the expected shape")


def _linkedin_job_with_poster(cfg) -> int:
    with session_scope(cfg.db_path) as session:
        company = Company(name="Acme", normalized_name="acme")
        session.add(company)
        session.flush()
        job = Job(
            external_id="li-1", source="linkedin", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
            company_id=company.id,
            poster_name="Jane Doe",
            poster_profile_url="https://www.linkedin.com/in/jane-doe",
        )
        session.add(job)
        session.flush()
        return job.id


def test_find_contacts_returns_the_posted_contact_and_the_searched_ones(client, cfg) -> None:
    job_id = _linkedin_job_with_poster(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(Contact(full_name="Priya Shah", profile_url="linkedin.com/in/priya", origin="manual"))

    app = client.app
    app.state.outreach_sender = FakeSenderWithClient([
        {"name": "Priya Shah", "headline": "Recruiter", "profile_url": "https://www.linkedin.com/in/priya"},
        {"name": "Sam Lee", "headline": "Engineering Manager", "profile_url": "https://www.linkedin.com/in/sam"},
    ])

    response = client.post(f"/api/outreach/{job_id}/find")
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert [c["origin"] for c in candidates] == ["job_poster", "company_search", "company_search"]
    assert candidates[0]["full_name"] == "Jane Doe"
    priya = next(c for c in candidates if c["full_name"] == "Priya Shah")
    assert priya["existing"] is True
    sam = next(c for c in candidates if c["full_name"] == "Sam Lee")
    assert sam["existing"] is False


class ExplodingSearchClient:
    """Any call at all is the bug this test is about."""

    def search_people(self, company, keywords, *, limit=5):
        raise AssertionError(f"searched LinkedIn for {company!r}, which is not a company")


def _anonymous_upwork_job(cfg) -> int:
    from jobhunt.pipeline.client_identity import COMPANY_NAME_PLACEHOLDER

    with session_scope(cfg.db_path) as session:
        company = Company(name=COMPANY_NAME_PLACEHOLDER, normalized_name="upwork client")
        session.add(company)
        session.flush()
        job = Job(
            external_id="up-1", source="upwork", market="upwork",
            title="RAG pipeline engineer", title_normalized="rag pipeline engineer",
            company_id=company.id,
        )
        session.add(job)
        session.flush()
        return job.id


def test_find_contacts_never_searches_linkedin_for_the_upwork_placeholder(client, cfg) -> None:
    """Most gigs name no client, so most Find contacts presses land here. Left
    alone, each one spends a people-search and shows whoever LinkedIn thinks
    "Upwork client" is, styled exactly like a real candidate."""
    job_id = _anonymous_upwork_job(cfg)
    client.app.state.outreach_sender = FakeSenderWithClient.__new__(FakeSenderWithClient)
    client.app.state.outreach_sender.client = ExplodingSearchClient()

    response = client.post(f"/api/outreach/{job_id}/find")
    assert response.status_code == 200
    body = response.json()
    assert body["candidates"] == []
    assert "nothing to search" in body["note"]


def test_find_contacts_still_searches_for_a_gig_with_a_recovered_client(client, cfg) -> None:
    job_id = _anonymous_upwork_job(cfg)
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        real = Company(name="Northquill", normalized_name="northquill")
        session.add(real)
        session.flush()
        job.company_id = real.id

    client.app.state.outreach_sender = FakeSenderWithClient([
        {"name": "Sam Lee", "headline": "Founder", "profile_url": "https://www.linkedin.com/in/sam"},
    ])
    body = client.post(f"/api/outreach/{job_id}/find").json()
    assert [c["full_name"] for c in body["candidates"]] == ["Sam Lee"]
    assert body["note"] is None


def test_find_contacts_on_an_unknown_job_is_a_404(client) -> None:
    assert client.post("/api/outreach/9999/find").status_code == 404


def test_find_contacts_with_a_sender_that_cannot_search_returns_the_stated_contact_only(
    client, cfg
) -> None:
    job_id = _linkedin_job_with_poster(cfg)
    # The default sender is the stub, which has no `.client` at all.
    response = client.post(f"/api/outreach/{job_id}/find")
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert [c["origin"] for c in candidates] == ["job_poster"]


def test_a_failed_search_still_returns_the_stated_contact_as_a_502(client, cfg) -> None:
    job_id = _linkedin_job_with_poster(cfg)
    client.app.state.outreach_sender = FakeSenderWithClient.__new__(FakeSenderWithClient)
    client.app.state.outreach_sender.client = BrokenSearchClient()

    response = client.post(f"/api/outreach/{job_id}/find")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "not the expected shape" in detail["message"]
    assert [c["full_name"] for c in detail["candidates"]] == ["Jane Doe"]


def test_stated_contacts_returns_the_poster_for_a_job_that_has_one(client, cfg) -> None:
    job_id = _linkedin_job_with_poster(cfg)
    response = client.get(f"/api/outreach/{job_id}/contacts/stated")
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert [c["origin"] for c in candidates] == ["job_poster"]
    assert candidates[0]["full_name"] == "Jane Doe"
    assert candidates[0]["profile_url"] == "https://www.linkedin.com/in/jane-doe"


def test_stated_contacts_is_empty_for_a_job_without_a_poster(client, job_id) -> None:
    response = client.get(f"/api/outreach/{job_id}/contacts/stated")
    assert response.status_code == 200
    assert response.json()["candidates"] == []


def test_stated_contacts_marks_an_existing_poster(client, cfg) -> None:
    job_id = _linkedin_job_with_poster(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(
            Contact(full_name="Jane Doe", profile_url="linkedin.com/in/jane-doe", origin="job_poster")
        )

    response = client.get(f"/api/outreach/{job_id}/contacts/stated")
    candidates = response.json()["candidates"]
    assert candidates[0]["existing"] is True


def test_stated_contacts_on_an_unknown_job_is_a_404(client) -> None:
    assert client.get("/api/outreach/9999/contacts/stated").status_code == 404


def test_stated_contacts_never_calls_search_people_even_when_the_sender_has_one(
    client, cfg
) -> None:
    """The stated read must be incapable of a people-search, not merely built to
    skip one - a sender that could search is attached here and never touched."""
    job_id = _linkedin_job_with_poster(cfg)
    calls: list[tuple[str, list[str]]] = []

    class SpySearchClient:
        def search_people(self, company, keywords, *, limit=5):
            calls.append((company, keywords))
            return []

    class SpySender:
        def __init__(self):
            self.client = SpySearchClient()

    client.app.state.outreach_sender = SpySender()
    response = client.get(f"/api/outreach/{job_id}/contacts/stated")
    assert response.status_code == 200
    assert calls == []


def test_a_found_candidate_keeps_its_origin_and_headline(client, job_id):
    """The drawer sends what `find` returned. Storing it as manual erased the
    difference between the person the posting named and a name typed by hand."""
    response = client.post(
        f"/api/outreach/{job_id}/contacts",
        json={
            "full_name": "Deniz Aksoy",
            "profile_url": "linkedin.com/in/deniz",
            "headline": "Engineering Manager at Acme",
            "origin": "company_search",
        },
    )
    assert response.status_code == 200
    contact = client.get(f"/api/outreach/{job_id}").json()["contacts"][0]
    assert contact["origin"] == "company_search"
    assert contact["headline"] == "Engineering Manager at Acme"


def test_a_contact_added_without_an_origin_is_still_manual(client, job_id):
    assert add(client, job_id).status_code == 200
    assert client.get(f"/api/outreach/{job_id}").json()["contacts"][0]["origin"] == "manual"


def test_an_invented_origin_is_refused(client, job_id):
    response = client.post(
        f"/api/outreach/{job_id}/contacts",
        json={"full_name": "Deniz Aksoy", "origin": "referred_by_a_friend"},
    )
    assert response.status_code == 422
