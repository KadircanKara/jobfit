"""Today's budget, and the refusal when it is spent.

Two separate budgets, not one: the daily invite and DM caps reset at midnight,
while InMail credits are a stock the account holds. Conflating them would let a
day of DMs quietly eat the credits, or a spent credit unblock itself tomorrow.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobhunt.config import Config
from jobhunt.db.models import Outreach, utcnow
from jobhunt.outreach import provider

INVITES = "invites"
DMS = "dms"
CREDITS = "credits"

_KINDS: dict[str, str] = {
    provider.INVITE_NOTE: INVITES,
    provider.INVITE_THEN_DM: INVITES,
    provider.DM: DMS,
    provider.FREE_INMAIL: DMS,
    provider.PAID_INMAIL: CREDITS,
}

_LABELS = {
    INVITES: "daily invite cap",
    DMS: "daily message cap",
    CREDITS: "InMail credits",
}


class CapReached(Exception):
    """A refusal, not a warning. Carries which budget ran out."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclasses.dataclass(frozen=True)
class Budget:
    invites_used: int
    invites_max: int
    dms_used: int
    dms_max: int
    credits: int
    delay_min: float
    delay_max: float


def kind_for(route: str) -> str:
    """Which budget this route spends."""
    return _KINDS[route]


def _day_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def _count(session: Session, column, routes: tuple[str, ...], now: dt.datetime) -> int:
    start, end = _day_bounds(now)
    return session.scalar(
        select(func.count())
        .select_from(Outreach)
        .where(Outreach.route.in_(routes), column >= start, column < end)
    ) or 0


def _credits_spent(session: Session) -> int:
    """Every paid InMail ever sent. Credits are a stock, so this does not reset."""
    return session.scalar(
        select(func.count())
        .select_from(Outreach)
        .where(Outreach.route == provider.PAID_INMAIL, Outreach.sent_at.is_not(None))
    ) or 0


def budget(config: Config, session: Session, *, now: dt.datetime | None = None) -> Budget:
    """Today's usage against the configured caps.

    "Today" is a UTC day: every timestamp in this database is naive UTC
    (jobhunt.db.models.utcnow), so the default clock must match or the day
    boundary drifts against the stored data.
    """
    now = now or utcnow()
    return Budget(
        invites_used=_count(
            session, Outreach.invited_at, (provider.INVITE_NOTE, provider.INVITE_THEN_DM), now
        ),
        invites_max=int(config.get("outreach", "max_daily_invites", default=20)),
        dms_used=_count(session, Outreach.sent_at, (provider.DM, provider.FREE_INMAIL), now),
        dms_max=int(config.get("outreach", "max_daily_dms", default=25)),
        credits=int(config.get("outreach", "inmail_credits", default=0)) - _credits_spent(session),
        delay_min=float(config.get("outreach", "invite_delay_min_seconds", default=15.0)),
        delay_max=float(config.get("outreach", "invite_delay_max_seconds", default=60.0)),
    )


def check(config: Config, session: Session, route: str, *, now: dt.datetime | None = None) -> None:
    """Raise CapReached if this route cannot go out right now."""
    now = now or utcnow()
    current = budget(config, session, now=now)
    kind = kind_for(route)
    if kind == INVITES and current.invites_used >= current.invites_max:
        raise CapReached(kind, f"{_LABELS[kind]} reached ({current.invites_max}). It resets at midnight.")
    if kind == DMS and current.dms_used >= current.dms_max:
        raise CapReached(kind, f"{_LABELS[kind]} reached ({current.dms_max}). It resets at midnight.")
    if kind == CREDITS and current.credits <= 0:
        raise CapReached(kind, "No InMail credits left. Use an invite instead.")


def spacing_seconds(
    config: Config, *, rand: Callable[[float, float], float] = random.uniform
) -> float:
    """How long to wait before the next send of the same kind."""
    low = float(config.get("outreach", "invite_delay_min_seconds", default=15.0))
    high = float(config.get("outreach", "invite_delay_max_seconds", default=60.0))
    return rand(low, high)
