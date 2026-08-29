"""Every write the outreach flow makes, in one place.

The transitions live here for the same reason `applications.TRANSITIONS` does:
a state machine spread across HTTP handlers is a state machine nobody can read.
`sent` is terminal on purpose - it is the record that a message reached a person,
and nothing should quietly rewrite that.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunt.config import Config
from jobhunt.db.models import Contact, Job, Outreach, utcnow
from jobhunt.db.session import session_scope
from jobhunt.outreach import caps, drafts, provider, routing

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "none": ("drafted", "cancelled"),
    "drafted": ("drafted", "queued", "sent", "failed", "cancelled"),
    "queued": ("sent", "failed", "cancelled"),
    "sent": (),
    "failed": ("drafted", "cancelled"),
    "cancelled": ("drafted",),
}


class UnknownJob(Exception):
    def __init__(self, job_id: int) -> None:
        super().__init__(str(job_id))
        self.job_id = job_id


class UnknownContact(Exception):
    def __init__(self, contact_id: int) -> None:
        super().__init__(str(contact_id))
        self.contact_id = contact_id


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"{current} -> {target}")
        self.current = current
        self.target = target


class TooLong(Exception):
    def __init__(self, length: int, limit: int) -> None:
        super().__init__(f"{length} characters, limit {limit}")
        self.length = length
        self.limit = limit


def _move(row: Outreach, target: str) -> None:
    if target not in TRANSITIONS[row.state]:
        raise IllegalTransition(row.state, target)
    row.state = target
    row.last_state_change = utcnow()


def _job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise UnknownJob(job_id)
    return job


def _row(session: Session, job_id: int, contact_id: int) -> Outreach:
    row = session.scalars(
        select(Outreach).where(Outreach.job_id == job_id, Outreach.contact_id == contact_id)
    ).first()
    if row is None:
        raise UnknownContact(contact_id)
    return row


def _payload(row: Outreach, contact: Contact, status: provider.ContactStatus) -> dict[str, Any]:
    # A forced route (the person connected, or gained free InMail) always wins over
    # whatever was drafted before that happened - the route must track status, not
    # the moment the draft was written. `body` is never touched here.
    current_route = routing.route_for(status) or row.route
    return {
        "contact_id": contact.id,
        "full_name": contact.full_name,
        "headline": contact.headline,
        "profile_url": contact.profile_url,
        "origin": contact.origin,
        "is_connection": contact.is_connection,
        "can_send_inmail": contact.can_send_inmail,
        "route": current_route,
        "allowed_routes": list(routing.allowed(status)),
        "needs_choice": routing.route_for(status) is None,
        "state": row.state,
        "body": row.body,
        "limit": drafts.limit_for(current_route),
        "provider_ref": row.provider_ref,
        "failure": row.failure,
        "invited_at": row.invited_at.isoformat() if row.invited_at else None,
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


def _budget_payload(config: Config, session: Session) -> dict[str, Any]:
    budget = caps.budget(config, session)
    return {
        "invites_used": budget.invites_used,
        "invites_max": budget.invites_max,
        "dms_used": budget.dms_used,
        "dms_max": budget.dms_max,
        "credits": budget.credits,
        "delay_min": budget.delay_min,
        "delay_max": budget.delay_max,
    }


def for_job(config: Config, job_id: int, sender: provider.LinkedInProvider) -> dict[str, Any]:
    """Every contact for this job, with the route each one currently resolves to."""
    with session_scope(config.db_path) as session:
        _job(session, job_id)
        rows = session.scalars(select(Outreach).where(Outreach.job_id == job_id)).all()
        contacts = []
        for row in rows:
            contact = session.get(Contact, row.contact_id)
            contacts.append(_payload(row, contact, sender.status(contact)))
        return {"contacts": contacts, "budget": _budget_payload(config, session)}


def add_contact(
    config: Config,
    job_id: int,
    *,
    full_name: str,
    profile_url: str | None = None,
    headline: str | None = None,
    origin: str = "manual",
) -> dict[str, Any]:
    """Attach a person to a job. An existing profile URL reuses that person."""
    with session_scope(config.db_path) as session:
        _job(session, job_id)
        contact = None
        if profile_url:
            contact = session.scalars(
                select(Contact).where(Contact.profile_url == profile_url)
            ).first()
        if contact is None:
            contact = Contact(
                full_name=full_name, profile_url=profile_url, headline=headline, origin=origin
            )
            session.add(contact)
            session.flush()
        existing = session.scalars(
            select(Outreach).where(Outreach.job_id == job_id, Outreach.contact_id == contact.id)
        ).first()
        row = existing or Outreach(job_id=job_id, contact_id=contact.id)
        if existing is None:
            session.add(row)
            session.flush()
        status = provider.ContactStatus(contact.is_connection, contact.can_send_inmail, 0)
        return _payload(row, contact, status)


def remove_contact(config: Config, contact_id: int) -> None:
    """Drop a contact that was never messaged. A sent row is a record, so it stays."""
    with session_scope(config.db_path) as session:
        rows = session.scalars(select(Outreach).where(Outreach.contact_id == contact_id)).all()
        if not rows:
            raise UnknownContact(contact_id)
        if any(row.state in ("sent", "queued") for row in rows):
            raise IllegalTransition("sent", "deleted")
        for row in rows:
            session.delete(row)
        contact = session.get(Contact, contact_id)
        if contact is not None:
            session.delete(contact)


def set_status(
    config: Config,
    contact_id: int,
    *,
    is_connection: bool | None = None,
    can_send_inmail: bool | None = None,
) -> dict[str, Any]:
    """Stub-only. The real provider makes this a read, not a write."""
    with session_scope(config.db_path) as session:
        contact = session.get(Contact, contact_id)
        if contact is None:
            raise UnknownContact(contact_id)
        if is_connection is not None:
            contact.is_connection = is_connection
        if can_send_inmail is not None:
            contact.can_send_inmail = can_send_inmail
        contact.status_checked_at = utcnow()
        return {
            "contact_id": contact.id,
            "is_connection": contact.is_connection,
            "can_send_inmail": contact.can_send_inmail,
        }


def _resolve_route(row: Outreach, status: provider.ContactStatus, route: str | None) -> str:
    """The route this operation runs on: the free one when there is one, else the pick."""
    forced = routing.route_for(status)
    if forced is not None:
        return forced
    chosen = route or row.route
    if chosen not in routing.allowed(status):
        raise IllegalTransition(row.state, "approve")
    return chosen


def draft(
    config: Config,
    job_id: int,
    contact_id: int,
    sender: provider.LinkedInProvider,
    *,
    route: str | None = None,
) -> dict[str, Any]:
    """Write the template into the row. Replaces whatever body was there."""
    with session_scope(config.db_path) as session:
        job = _job(session, job_id)
        row = _row(session, job_id, contact_id)
        contact = session.get(Contact, contact_id)
        status = sender.status(contact)
        chosen = _resolve_route(row, status, route)
        if "drafted" not in TRANSITIONS[row.state]:
            raise IllegalTransition(row.state, "drafted")
        row.route = chosen
        row.body = drafts.template(job, contact, chosen)
        row.drafted_at = utcnow()
        _move(row, "drafted")
        return _payload(row, contact, status)


def save_body(config: Config, job_id: int, contact_id: int, body: str) -> dict[str, Any]:
    """Store an edited message. Never changes the route, and never refuses on length.

    Length is judged at approval, not while typing: a body caught mid-edit is not
    a message anyone tried to send.
    """
    with session_scope(config.db_path) as session:
        row = _row(session, job_id, contact_id)
        contact = session.get(Contact, contact_id)
        row.body = body
        if row.state == "none":
            # A row with a saved body is drafted, whether or not `draft()` ever ran.
            # Only "none" promotes here - any other state is left alone, so this
            # never becomes a second way to change state.
            _move(row, "drafted")
        status = provider.ContactStatus(contact.is_connection, contact.can_send_inmail, 0)
        return _payload(row, contact, status)


def approve(
    config: Config,
    job_id: int,
    contact_id: int,
    sender: provider.LinkedInProvider,
    *,
    route: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Send this one message to this one person, or queue it behind an invite."""
    with session_scope(config.db_path) as session:
        row = _row(session, job_id, contact_id)
        contact = session.get(Contact, contact_id)
        status = sender.status(contact)
        chosen = _resolve_route(row, status, route)
        target = "queued" if chosen == provider.INVITE_THEN_DM else "sent"
        if target not in TRANSITIONS[row.state]:
            raise IllegalTransition(row.state, target)
        body = row.body or ""
        if drafts.over_limit(body, chosen):
            raise TooLong(len(body), drafts.limit_for(chosen))
        caps.check(config, session, chosen, now=now)

        # Nothing above this line has told the provider or the row anything - a
        # refusal up to here leaves both untouched. Everything below only runs
        # once the transition is known to be legal, so a provider call can never
        # be discarded by a rollback the way an unvalidated `_move` would.
        row.route = chosen
        row.approved_at = utcnow()
        if chosen == provider.INVITE_THEN_DM:
            result = sender.send_invite(contact, None)
            row.invited_at = utcnow()
            row.provider_ref = result.ref
            _move(row, "queued")
        else:
            result = _send(sender, contact, chosen, body)
            row.provider_ref = result.ref
            if chosen in (provider.INVITE_NOTE,):
                row.invited_at = utcnow()
            row.sent_at = utcnow()
            _move(row, "sent")
        return _payload(row, contact, status)


def _send(
    sender: provider.LinkedInProvider, contact: Contact, route: str, body: str
) -> provider.SendResult:
    if route == provider.DM:
        return sender.send_dm(contact, body)
    if route == provider.FREE_INMAIL:
        return sender.send_inmail(contact, body, paid=False)
    if route == provider.PAID_INMAIL:
        return sender.send_inmail(contact, body, paid=True)
    return sender.send_invite(contact, body)


def cancel(config: Config, job_id: int, contact_id: int) -> dict[str, Any]:
    """Stop a drafted or queued attempt. An invite already sent cannot be unsent."""
    with session_scope(config.db_path) as session:
        row = _row(session, job_id, contact_id)
        contact = session.get(Contact, contact_id)
        _move(row, "cancelled")
        status = provider.ContactStatus(contact.is_connection, contact.can_send_inmail, 0)
        return _payload(row, contact, status)
