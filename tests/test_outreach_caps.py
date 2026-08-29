"""What the account is allowed to do today.

The numbers are deliberately below the user's own manual peak. Automation that
matches a human's busiest day is the pattern that draws attention; automation
that stays well under it does not. Credits are a separate budget from the daily
DM cap and are checked on their own.
"""
from __future__ import annotations

import datetime as dt

import pytest

from jobhunt.db.models import Contact, Job, Outreach
from jobhunt.db.session import session_scope
from jobhunt.outreach import caps, provider

NOW = dt.datetime(2026, 8, 29, 14, 0, 0)


def seed(cfg, rows: list[tuple[str, str, dt.datetime]]) -> None:
    """Each row is (route, timestamp_column, when)."""
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="ext-1", source="greenhouse", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
        )
        session.add(job)
        session.flush()
        for index, (route, column, when) in enumerate(rows):
            contact = Contact(full_name=f"Person {index}", origin="manual")
            session.add(contact)
            session.flush()
            row = Outreach(job_id=job.id, contact_id=contact.id, route=route, state="sent")
            setattr(row, column, when)
            session.add(row)


def test_kind_for_maps_every_route():
    assert caps.kind_for(provider.INVITE_NOTE) == "invites"
    assert caps.kind_for(provider.INVITE_THEN_DM) == "invites"
    assert caps.kind_for(provider.DM) == "dms"
    assert caps.kind_for(provider.FREE_INMAIL) == "dms"
    assert caps.kind_for(provider.PAID_INMAIL) == "credits"


def test_budget_counts_only_todays_rows(cfg):
    yesterday = NOW - dt.timedelta(days=1)
    seed(cfg, [
        (provider.INVITE_NOTE, "invited_at", NOW),
        (provider.INVITE_NOTE, "invited_at", yesterday),
        (provider.DM, "sent_at", NOW),
    ])
    with session_scope(cfg.db_path) as session:
        budget = caps.budget(cfg, session, now=NOW)
    assert budget.invites_used == 1
    assert budget.dms_used == 1


def test_check_passes_under_the_cap(cfg):
    with session_scope(cfg.db_path) as session:
        caps.check(cfg, session, provider.DM, now=NOW)  # does not raise


def test_check_refuses_at_the_invite_cap(cfg):
    cfg.raw["outreach"]["max_daily_invites"] = 1
    seed(cfg, [(provider.INVITE_NOTE, "invited_at", NOW)])
    with session_scope(cfg.db_path) as session:
        with pytest.raises(caps.CapReached) as excinfo:
            caps.check(cfg, session, provider.INVITE_THEN_DM, now=NOW)
    assert excinfo.value.kind == "invites"


def test_the_dm_cap_does_not_block_an_invite(cfg):
    cfg.raw["outreach"]["max_daily_dms"] = 1
    seed(cfg, [(provider.DM, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        caps.check(cfg, session, provider.INVITE_NOTE, now=NOW)  # does not raise
        with pytest.raises(caps.CapReached):
            caps.check(cfg, session, provider.FREE_INMAIL, now=NOW)


def test_paid_inmail_is_refused_with_no_credits_left(cfg):
    cfg.raw["outreach"]["inmail_credits"] = 0
    with session_scope(cfg.db_path) as session:
        with pytest.raises(caps.CapReached) as excinfo:
            caps.check(cfg, session, provider.PAID_INMAIL, now=NOW)
    assert excinfo.value.kind == "credits"


def test_credits_fall_as_paid_inmails_go_out(cfg):
    cfg.raw["outreach"]["inmail_credits"] = 3
    seed(cfg, [(provider.PAID_INMAIL, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        assert caps.budget(cfg, session, now=NOW).credits == 2


def test_spacing_stays_inside_the_configured_window(cfg):
    low = caps.spacing_seconds(cfg, rand=lambda a, b: a)
    high = caps.spacing_seconds(cfg, rand=lambda a, b: b)
    assert low == cfg.get("outreach", "invite_delay_min_seconds")
    assert high == cfg.get("outreach", "invite_delay_max_seconds")
