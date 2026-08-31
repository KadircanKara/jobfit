"""Today's budget, and the refusal when it is spent.

Three separate budgets, not one: the daily invite and DM caps reset at UTC
midnight, InMail credits are a stock the account holds, and the weekly invite
ceiling is a rolling seven-day window on top of the daily one. Conflating the
first two would let a day of DMs quietly eat the credits, or a spent credit
unblock itself tomorrow. The weekly ceiling exists on its own because LinkedIn
restricts accounts over invite volume specifically, and the commonly cited safe
band - 100 to 200 a week - sits well under what the daily cap of 20 allows
across seven enthusiastic days; the daily reset alone would never catch that.

Spacing is the fourth refusal here rather than a caller's courtesy: it binds on
the poller, which can otherwise release several queued DMs in the same second.
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
INVITES_WEEK = "invites_week"

_KINDS: dict[str, str] = {
    provider.INVITE_NOTE: INVITES,
    provider.INVITE_THEN_DM: INVITES,
    provider.DM: DMS,
    provider.FREE_INMAIL: DMS,
    provider.PAID_INMAIL: CREDITS,
}

# What a send of this route has to get past. A paid InMail is a message like any
# other, so it spends the daily cap *and* a credit; `invite_then_dm` spends only
# an invite here, because its DM is checked again on the day the poller releases it.
_CHECKED: dict[str, tuple[str, ...]] = {
    provider.INVITE_NOTE: (INVITES, INVITES_WEEK),
    provider.INVITE_THEN_DM: (INVITES, INVITES_WEEK),
    provider.DM: (DMS,),
    provider.FREE_INMAIL: (DMS,),
    provider.PAID_INMAIL: (DMS, CREDITS),
}

_INVITE_ROUTES = (provider.INVITE_NOTE, provider.INVITE_THEN_DM)
# `invite_then_dm` counts twice over two days: as an invite when it goes out, and
# as a DM on the day the poller actually releases the message. Two budgets, two
# days - leaving it out of the DM side let the deferred path escape the cap.
_DM_ROUTES = (provider.DM, provider.FREE_INMAIL, provider.PAID_INMAIL, provider.INVITE_THEN_DM)

# Which column records "when a send of this kind last happened", for spacing.
_LAST_SEND = {
    INVITES: (Outreach.invited_at, _INVITE_ROUTES),
    DMS: (Outreach.sent_at, _DM_ROUTES),
}

_LABELS = {
    INVITES: "daily invite cap",
    DMS: "daily message cap",
    CREDITS: "InMail credits",
    INVITES_WEEK: "weekly invite cap",
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
    invites_week_used: int
    invites_week_max: int
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


def _count_since(session: Session, column, routes: tuple[str, ...], since: dt.datetime) -> int:
    return session.scalar(
        select(func.count()).select_from(Outreach).where(Outreach.route.in_(routes), column >= since)
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
        invites_used=_count(session, Outreach.invited_at, _INVITE_ROUTES, now),
        invites_max=int(config.get("outreach", "max_daily_invites", default=20)),
        invites_week_used=_count_since(
            session, Outreach.invited_at, _INVITE_ROUTES, now - dt.timedelta(days=7)
        ),
        invites_week_max=int(config.get("outreach", "max_weekly_invites", default=100)),
        dms_used=_count(session, Outreach.sent_at, _DM_ROUTES, now),
        dms_max=int(config.get("outreach", "max_daily_dms", default=25)),
        credits=int(config.get("outreach", "inmail_credits", default=0)) - _credits_spent(session),
        delay_min=float(config.get("outreach", "invite_delay_min_seconds", default=15.0)),
        delay_max=float(config.get("outreach", "invite_delay_max_seconds", default=60.0)),
    )


def _last_send_at(session: Session, kind: str) -> dt.datetime | None:
    column, routes = _LAST_SEND[kind]
    return session.scalar(select(func.max(column)).select_from(Outreach).where(Outreach.route.in_(routes)))


def _spacing_kind(route: str) -> str:
    return INVITES if route in _INVITE_ROUTES else DMS


def check(
    config: Config,
    session: Session,
    route: str,
    *,
    now: dt.datetime | None = None,
    rand: Callable[[float, float], float] = random.uniform,
) -> None:
    """Raise CapReached if this route cannot go out right now."""
    now = now or utcnow()
    current = budget(config, session, now=now)
    for kind in _CHECKED[route]:
        if kind == INVITES and current.invites_used >= current.invites_max:
            raise CapReached(
                kind, f"{_LABELS[kind]} reached ({current.invites_max}). It resets at midnight UTC."
            )
        if kind == INVITES_WEEK and current.invites_week_used >= current.invites_week_max:
            raise CapReached(
                kind,
                f"{_LABELS[kind]} reached ({current.invites_week_max}). "
                "LinkedIn restricts accounts over invite volume, so this one is a rolling week.",
            )
        if kind == DMS and current.dms_used >= current.dms_max:
            raise CapReached(
                kind, f"{_LABELS[kind]} reached ({current.dms_max}). It resets at midnight UTC."
            )
        if kind == CREDITS and current.credits <= 0:
            raise CapReached(kind, "No InMail credits left. Use an invite instead.")
    _check_spacing(config, session, route, now=now, rand=rand)


def _check_spacing(
    config: Config,
    session: Session,
    route: str,
    *,
    now: dt.datetime,
    rand: Callable[[float, float], float],
) -> None:
    """Refuse a send that would follow the last one of its kind too closely."""
    kind = _spacing_kind(route)
    last = _last_send_at(session, kind)
    if last is None:
        return
    gap = spacing_seconds(config, rand=rand)
    elapsed = (now - last).total_seconds()
    if elapsed >= gap:
        return
    word = "invite" if kind == INVITES else "message"
    raise CapReached(
        "spacing",
        f"The last {word} went out {elapsed:.0f}s ago. "
        f"Wait about {gap - elapsed:.0f}s more before the next one.",
    )


def spacing_seconds(
    config: Config, *, rand: Callable[[float, float], float] = random.uniform
) -> float:
    """How long to wait before the next send of the same kind."""
    low = float(config.get("outreach", "invite_delay_min_seconds", default=15.0))
    high = float(config.get("outreach", "invite_delay_max_seconds", default=60.0))
    return rand(low, high)
