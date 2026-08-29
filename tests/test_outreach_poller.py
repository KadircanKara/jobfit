"""The queue behind an invite.

A queued DM is the one place a press now causes a send later, so every path out
of `queued` has to be exercised: accepted and released, accepted but capped, and
never accepted at all.
"""
from __future__ import annotations

import datetime as dt

from jobhunt.db.models import Contact, Job, Outreach, utcnow
from jobhunt.db.session import session_scope
from jobhunt.outreach import poller, provider, service, stub

# Derived from the real clock, not a literal: `queued_row` stamps `invited_at`
# through `service.approve`, which uses `utcnow()`. A literal date would only
# pass while the system clock happened to agree with it.
NOW = utcnow()


def queued_row(cfg) -> tuple[int, int, stub.StubProvider]:
    sender = stub.StubProvider(cfg)
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="ext-1", source="greenhouse", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
        )
        contact = Contact(full_name="Ece Yurdakul", origin="manual",
                          is_connection=False, can_send_inmail=False)
        session.add_all([job, contact])
        session.flush()
        job_id, contact_id = job.id, contact.id
    with session_scope(cfg.db_path) as session:
        session.add(Outreach(job_id=job_id, contact_id=contact_id))
    service.draft(cfg, job_id, contact_id, sender, route=provider.INVITE_THEN_DM)
    service.approve(cfg, job_id, contact_id, sender, route=provider.INVITE_THEN_DM)
    return job_id, contact_id, sender


def state_of(cfg) -> str:
    with session_scope(cfg.db_path) as session:
        return session.query(Outreach).one().state


def test_an_unaccepted_invite_stays_queued(cfg):
    queued_row(cfg)
    counts = poller.tick(cfg, stub.StubProvider(cfg), now=NOW)
    assert counts == {"released": 0, "expired": 0}
    assert state_of(cfg) == "queued"


def test_acceptance_releases_the_stored_dm(cfg):
    _, contact_id, sender = queued_row(cfg)
    sender.accept(contact_id)
    counts = poller.tick(cfg, sender, now=NOW)
    assert counts["released"] == 1
    assert state_of(cfg) == "sent"


def test_a_capped_release_stays_queued_for_the_next_tick(cfg):
    _, contact_id, sender = queued_row(cfg)
    sender.accept(contact_id)
    cfg.raw["outreach"]["max_daily_dms"] = 0
    assert poller.tick(cfg, sender, now=NOW)["released"] == 0
    assert state_of(cfg) == "queued"
    cfg.raw["outreach"]["max_daily_dms"] = 25
    assert poller.tick(cfg, sender, now=NOW)["released"] == 1
    assert state_of(cfg) == "sent"


def test_an_invite_ignored_past_the_window_is_cancelled(cfg):
    queued_row(cfg)
    later = NOW + dt.timedelta(days=22)
    counts = poller.tick(cfg, stub.StubProvider(cfg), now=later)
    assert counts["expired"] == 1
    assert state_of(cfg) == "cancelled"


def test_a_cancelled_row_is_left_alone(cfg):
    job_id, contact_id, sender = queued_row(cfg)
    service.cancel(cfg, job_id, contact_id)
    sender.accept(contact_id)
    assert poller.tick(cfg, sender, now=NOW) == {"released": 0, "expired": 0}
    assert state_of(cfg) == "cancelled"
