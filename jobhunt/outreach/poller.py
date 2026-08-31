"""Releasing the DM once the invite is accepted.

Follows the shape `jobhunt/web/idle.py` already uses: a daemon thread, an
injected clock, and a check interval kept separate from the deadline so a test
never waits. A cap refusal leaves the row queued rather than failing it - the
message is still wanted, just not today.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy import select

from jobhunt.config import Config
from jobhunt.db.models import Contact, Outreach, utcnow
from jobhunt.db.session import session_scope
from jobhunt.outreach import caps, provider, service

log = logging.getLogger(__name__)


def tick(
    config: Config, sender: provider.LinkedInProvider, *, now: dt.datetime | None = None
) -> dict[str, int]:
    """One pass over the queue. Returns what it released and what it expired.

    Every timestamp in this database is naive UTC (jobhunt.db.models.utcnow), so
    the default clock must match it or the window comparison drifts against the
    stored data - the same reasoning caps.budget already applies to "today".
    """
    now = now or utcnow()
    window = dt.timedelta(days=int(config.get("outreach", "poll_window_days", default=21)))
    released = 0
    expired = 0

    with session_scope(config.db_path) as session:
        rows = session.scalars(select(Outreach).where(Outreach.state == "queued")).all()
        pending = [(row.job_id, row.contact_id, row.invited_at) for row in rows]

    for job_id, contact_id, invited_at in pending:
        with session_scope(config.db_path) as session:
            contact = session.get(Contact, contact_id)
            accepted = sender.invite_accepted(contact) if contact is not None else False

        if not accepted:
            if invited_at is not None and now - invited_at > window:
                with session_scope(config.db_path) as session:
                    row = session.scalars(
                        select(Outreach).where(
                            Outreach.job_id == job_id, Outreach.contact_id == contact_id
                        )
                    ).one()
                    if row.state != "queued":
                        # Left alone if it moved on (or was cancelled) since we listed it.
                        continue
                    row.failure = "Invite was not accepted within the window."
                    service._move(row, "cancelled")
                expired += 1
            continue

        try:
            sent = _release(config, sender, job_id, contact_id, now=now)
        except caps.CapReached as refusal:
            # Still wanted, just not today. Left queued for the next tick.
            log.info("outreach queue held: %s", refusal.message)
            continue
        if sent:
            released += 1

    return {"released": released, "expired": expired}


def _release(
    config: Config,
    sender: provider.LinkedInProvider,
    job_id: int,
    contact_id: int,
    *,
    now: dt.datetime,
) -> bool:
    """Send the stored DM now that the invite is accepted.

    Returns whether it actually sent. `tick`'s released count is the poller's
    only external signal, so a guard that quietly no-ops (the row was resolved
    by something else between listing and this call) must report False rather
    than let the caller assume every non-raising call was a send.
    """
    with session_scope(config.db_path) as session:
        row = session.scalars(
            select(Outreach).where(Outreach.job_id == job_id, Outreach.contact_id == contact_id)
        ).one()
        if row.state != "queued":
            # A row already cancelled (or otherwise moved) is left alone even if
            # the invite is later accepted - the user's cancel wins.
            return False
        contact = session.get(Contact, contact_id)
        caps.check(config, session, provider.DM, now=now)
        try:
            # Claimed before the send: another tick, or an HTTP approve on the same
            # queued row, may have taken it between the read above and this line.
            service._claim(session, row, "sent")
        except service.IllegalTransition:
            return False
        result = sender.send_dm(contact, row.body or "")
        row.accepted_at = row.accepted_at or utcnow()
        if not result.ok:
            # The claim above is provisional until this transaction commits, so
            # correcting it to `failed` here is safe: nobody else can have read
            # this row as `sent` yet, and a refused/erroring send must not be
            # recorded as one that reached the person.
            row.state = "failed"
            row.last_state_change = utcnow()
            row.failure = result.failure
            return False
        row.provider_ref = result.ref
        row.sent_at = utcnow()
        return True


def watch(
    config: Config,
    sender: provider.LinkedInProvider,
    *,
    interval: float | None = None,
    stop: threading.Event | None = None,
) -> threading.Thread:
    """Run the queue in the background for as long as the server lives."""
    every = interval or float(config.get("outreach", "poll_interval_seconds", default=3600.0))
    halt = stop or threading.Event()

    def loop() -> None:
        while not halt.wait(every):
            try:
                tick(config, sender)
            except Exception:  # noqa: BLE001 - a poller that dies silently is worse
                log.exception("outreach poll failed")

    thread = threading.Thread(target=loop, daemon=True, name="jobhunt-outreach")
    thread.start()
    return thread
