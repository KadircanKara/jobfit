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
