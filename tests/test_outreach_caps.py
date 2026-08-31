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


def zero(low: float, high: float) -> float:
    """No spacing, for the tests that are about a cap rather than the clock."""
    return 0.0


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
            caps.check(cfg, session, provider.INVITE_THEN_DM, now=NOW, rand=zero)
    assert excinfo.value.kind == "invites"


def test_the_dm_cap_does_not_block_an_invite(cfg):
    cfg.raw["outreach"]["max_daily_dms"] = 1
    seed(cfg, [(provider.DM, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        caps.check(cfg, session, provider.INVITE_NOTE, now=NOW, rand=zero)  # does not raise
        with pytest.raises(caps.CapReached):
            caps.check(cfg, session, provider.FREE_INMAIL, now=NOW, rand=zero)


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


def test_a_released_dm_counts_against_the_daily_message_cap(cfg):
    """The deferred path spends two budgets on two days, and neither escapes.

    A poller release leaves `route` as invite_then_dm, so counting only dm and
    free_inmail let N accepted invites release N DMs with the counter still at 0.
    """
    seed(cfg, [(provider.INVITE_THEN_DM, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        assert caps.budget(cfg, session, now=NOW).dms_used == 1


def test_a_queued_invite_counts_as_an_invite_on_the_day_it_went_out(cfg):
    seed(cfg, [(provider.INVITE_THEN_DM, "invited_at", NOW)])
    with session_scope(cfg.db_path) as session:
        budget = caps.budget(cfg, session, now=NOW)
    assert budget.invites_used == 1
    assert budget.dms_used == 0


def test_paid_inmail_is_refused_at_the_daily_message_cap(cfg):
    """Credits are checked in addition to the cap, not instead of it."""
    cfg.raw["outreach"]["max_daily_dms"] = 1
    cfg.raw["outreach"]["inmail_credits"] = 12
    seed(cfg, [(provider.DM, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        with pytest.raises(caps.CapReached) as excinfo:
            caps.check(cfg, session, provider.PAID_INMAIL, now=NOW, rand=zero)
    assert excinfo.value.kind == "dms"


def test_a_paid_inmail_counts_against_the_daily_message_cap(cfg):
    seed(cfg, [(provider.PAID_INMAIL, "sent_at", NOW)])
    with session_scope(cfg.db_path) as session:
        assert caps.budget(cfg, session, now=NOW).dms_used == 1


def test_a_send_inside_the_spacing_window_is_refused(cfg):
    cfg.raw["outreach"]["invite_delay_min_seconds"] = 30.0
    cfg.raw["outreach"]["invite_delay_max_seconds"] = 30.0
    seed(cfg, [(provider.DM, "sent_at", NOW - dt.timedelta(seconds=10))])
    with session_scope(cfg.db_path) as session:
        with pytest.raises(caps.CapReached) as excinfo:
            caps.check(cfg, session, provider.DM, now=NOW)
    assert excinfo.value.kind == "spacing"
    assert "20s" in excinfo.value.message


def test_a_send_past_the_spacing_window_is_allowed(cfg):
    cfg.raw["outreach"]["invite_delay_min_seconds"] = 30.0
    cfg.raw["outreach"]["invite_delay_max_seconds"] = 30.0
    seed(cfg, [(provider.DM, "sent_at", NOW - dt.timedelta(seconds=31))])
    with session_scope(cfg.db_path) as session:
        caps.check(cfg, session, provider.DM, now=NOW)  # does not raise


def test_spacing_only_binds_within_one_kind(cfg):
    """An invite and a DM are different queues; one must not delay the other."""
    cfg.raw["outreach"]["invite_delay_min_seconds"] = 3600.0
    cfg.raw["outreach"]["invite_delay_max_seconds"] = 3600.0
    seed(cfg, [(provider.INVITE_NOTE, "invited_at", NOW - dt.timedelta(seconds=1))])
    with session_scope(cfg.db_path) as session:
        caps.check(cfg, session, provider.DM, now=NOW)  # does not raise
        with pytest.raises(caps.CapReached):
            caps.check(cfg, session, provider.INVITE_NOTE, now=NOW)


def test_spacing_draws_from_the_configured_window(cfg):
    low = caps.spacing_seconds(cfg, rand=lambda a, b: a)
    high = caps.spacing_seconds(cfg, rand=lambda a, b: b)
    assert low == cfg.get("outreach", "invite_delay_min_seconds")
    assert high == cfg.get("outreach", "invite_delay_max_seconds")


def test_the_weekly_invite_ceiling_refuses_the_next_invite(cfg):
    rows = [
        (provider.INVITE_NOTE, "invited_at", NOW - dt.timedelta(days=day))
        for day in range(7)
        for _ in range(15)
    ]
    seed(cfg, rows)
    with session_scope(cfg.db_path) as session:
        with pytest.raises(caps.CapReached) as excinfo:
            caps.check(cfg, session, provider.INVITE_NOTE, now=NOW, rand=zero)
    assert excinfo.value.kind in ("invites", "invites_week")


def test_an_invite_from_eight_days_ago_does_not_count(cfg):
    seed(cfg, [(provider.INVITE_NOTE, "invited_at", NOW - dt.timedelta(days=8))])
    with session_scope(cfg.db_path) as session:
        assert caps.budget(cfg, session, now=NOW).invites_week_used == 0


def test_the_budget_reports_both_invite_windows(cfg):
    with session_scope(cfg.db_path) as session:
        current = caps.budget(cfg, session, now=NOW)
    assert current.invites_max == 20
    assert current.invites_week_max == 100


def test_a_failed_invite_does_not_consume_the_weekly_cap(cfg):
    """Task 11 leaves a failed send's invited_at unset; the weekly count must

    follow the same timestamp column as the daily one, or a failure that is
    safe against the daily cap would quietly start costing the weekly one.
    """
    with session_scope(cfg.db_path) as session:
        job = Job(
            external_id="ext-1", source="greenhouse", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
        )
        session.add(job)
        session.flush()
        contact = Contact(full_name="Person 0", origin="manual")
        session.add(contact)
        session.flush()
        session.add(
            Outreach(
                job_id=job.id, contact_id=contact.id, route=provider.INVITE_NOTE,
                state="failed", invited_at=None, failure="boom",
            )
        )
        session.flush()
        assert caps.budget(cfg, session, now=NOW).invites_week_used == 0
        caps.check(cfg, session, provider.INVITE_NOTE, now=NOW, rand=zero)  # does not raise
