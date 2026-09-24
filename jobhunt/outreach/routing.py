"""The decision tree, spec section 5. Pure: a status in, a route out.

No database and no provider, so the branch that matters can be read and tested
without either. The order is the whole point — a free route always beats a paid
one, and a route that spends nothing beats one that spends a credit.
"""
from __future__ import annotations

from jobhunt.outreach import provider


def route_for(status: provider.ContactStatus) -> str | None:
    """The single route LinkedIn leaves open, or None when the user must choose."""
    if status.is_connection:
        return provider.DM
    if status.can_send_inmail:
        return provider.FREE_INMAIL
    return None


def allowed(status: provider.ContactStatus) -> tuple[str, ...]:
    """Every route the user may pick right now, in the order to show them."""
    single = route_for(status)
    if single is not None:
        return (single,)
    if status.inmail_credits <= 0:
        # Offering a paid InMail with no credits is offering a button that can
        # only fail. Drop it rather than disable it.
        return (provider.INVITE_NOTE, provider.INVITE_THEN_DM)
    return provider.FALLBACK_ROUTES
