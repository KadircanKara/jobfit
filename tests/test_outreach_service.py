"""Outreach rows: one attempt per person per job, and the states it moves through.

The unit the user approves is a job-and-contact pair, so the schema has to make
a second live attempt at the same person about the same job impossible rather
than merely discouraged.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from jobhunt.db.models import Contact, Job, Outreach
from jobhunt.db.session import session_scope
from jobhunt.outreach import caps, provider, service, stub


def make_job(cfg, external_id: str = "ext-1") -> int:
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id=external_id,
            source="greenhouse",
            market="global_remote",
            title="Backend Engineer",
            title_normalized="backend engineer",
            is_active=True,
        )
        session.add(job)
        session.flush()
        return job.id


def make_contact(cfg, name: str = "Deniz Aksoy", url: str | None = "linkedin.com/in/denizaksoy") -> int:
    with session_scope(cfg.db_path) as session:
        contact = Contact(full_name=name, profile_url=url, origin="manual")
        session.add(contact)
        session.flush()
        return contact.id


def test_outreach_defaults_to_no_route_and_no_state(cfg):
    job_id = make_job(cfg)
    contact_id = make_contact(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    with session_scope(cfg.db_path) as session:
        row = session.query(Outreach).one()
        assert row.state == "none"
        assert row.route is None
        assert row.body is None


def test_one_outreach_row_per_job_and_contact(cfg):
    job_id = make_job(cfg)
    contact_id = make_contact(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    with pytest.raises(IntegrityError):
        with session_scope(cfg.db_path) as session:
            session.add(Outreach(job_id=job_id, contact_id=contact_id))


def test_profile_url_is_unique_across_contacts(cfg):
    make_contact(cfg)
    with pytest.raises(IntegrityError):
        make_contact(cfg, name="Someone Else")


def test_contacts_without_a_profile_url_do_not_collide(cfg):
    make_contact(cfg, name="No URL One", url=None)
    make_contact(cfg, name="No URL Two", url=None)
    with session_scope(cfg.db_path) as session:
        assert session.query(Contact).count() == 2


def sender(cfg) -> stub.StubProvider:
    return stub.StubProvider(cfg)


def connected_contact(cfg, **flags) -> int:
    contact_id = make_contact(cfg, url=None)
    with session_scope(cfg.db_path) as session:
        contact = session.get(Contact, contact_id)
        for key, value in flags.items():
            setattr(contact, key, value)
    return contact_id


def test_adding_a_contact_creates_the_outreach_row(cfg):
    job_id = make_job(cfg)
    body = service.add_contact(cfg, job_id, full_name="Marit Lindqvist")
    assert body["full_name"] == "Marit Lindqvist"
    assert body["state"] == "none"
    assert body["origin"] == "manual"


def test_adding_the_same_profile_twice_reuses_the_person(cfg):
    first = make_job(cfg, "ext-1")
    second = make_job(cfg, "ext-2")
    url = "linkedin.com/in/marit"
    a = service.add_contact(cfg, first, full_name="Marit", profile_url=url)
    b = service.add_contact(cfg, second, full_name="Marit", profile_url=url)
    assert a["contact_id"] == b["contact_id"]
    with session_scope(cfg.db_path) as session:
        assert session.query(Outreach).count() == 2


def test_a_connection_drafts_against_the_dm_route(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=True)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    body = service.draft(cfg, job_id, contact_id, sender(cfg))
    assert body["route"] == provider.DM
    assert body["state"] == "drafted"
    assert body["body"]


def test_an_edited_body_survives_a_route_change(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=False, can_send_inmail=False)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_THEN_DM)
    service.save_body(cfg, job_id, contact_id, "three careful sentences")
    service.set_status(cfg, contact_id, is_connection=True)
    body = service.for_job(cfg, job_id, sender(cfg))["contacts"][0]
    assert body["body"] == "three careful sentences"
    assert body["route"] == provider.DM


def test_a_body_over_the_note_limit_is_refused(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=False, can_send_inmail=False)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.save_body(cfg, job_id, contact_id, "x" * 400)
    with pytest.raises(service.TooLong):
        service.approve(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_NOTE)


def test_approving_a_dm_marks_it_sent(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=True)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg))
    body = service.approve(cfg, job_id, contact_id, sender(cfg))
    assert body["state"] == "sent"
    assert body["provider_ref"].startswith("stub:")


def test_approving_invite_then_dm_queues_rather_than_sends(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=False, can_send_inmail=False)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_THEN_DM)
    body = service.approve(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_THEN_DM)
    assert body["state"] == "queued"
    assert body["invited_at"] is not None
    assert body["sent_at"] is None


def test_a_sent_row_cannot_be_approved_again(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=True)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg))
    service.approve(cfg, job_id, contact_id, sender(cfg))
    with pytest.raises(service.IllegalTransition):
        service.approve(cfg, job_id, contact_id, sender(cfg))


def test_approving_at_the_cap_is_refused(cfg):
    cfg.raw["outreach"]["max_daily_dms"] = 0
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=True)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg))
    with pytest.raises(caps.CapReached):
        service.approve(cfg, job_id, contact_id, sender(cfg))


def test_cancelling_a_queued_row_stops_the_deferred_send(cfg):
    job_id = make_job(cfg)
    contact_id = connected_contact(cfg, is_connection=False, can_send_inmail=False)
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_THEN_DM)
    service.approve(cfg, job_id, contact_id, sender(cfg), route=provider.INVITE_THEN_DM)
    body = service.cancel(cfg, job_id, contact_id)
    assert body["state"] == "cancelled"


def test_an_unknown_job_is_refused(cfg):
    with pytest.raises(service.UnknownJob):
        service.add_contact(cfg, 9999, full_name="Nobody")
