"""Which route LinkedIn actually leaves open, given who the contact is.

The tree is not advice. Where a free route exists it is the route; the user only
chooses where LinkedIn genuinely offers three different trade-offs.
"""
from __future__ import annotations

from jobhunt.outreach import provider, routing


def status(connection=None, inmail=None, credits=12) -> provider.ContactStatus:
    return provider.ContactStatus(
        is_connection=connection, can_send_inmail=inmail, inmail_credits=credits
    )


def test_a_connection_gets_a_regular_dm():
    assert routing.route_for(status(connection=True)) == provider.DM


def test_a_connection_wins_even_when_inmail_is_open():
    assert routing.route_for(status(connection=True, inmail=True)) == provider.DM


def test_a_stranger_with_open_inmail_gets_the_free_inmail():
    assert routing.route_for(status(connection=False, inmail=True)) == provider.FREE_INMAIL


def test_a_stranger_without_open_inmail_has_no_single_route():
    assert routing.route_for(status(connection=False, inmail=False)) is None


def test_unknown_status_takes_the_conservative_branch():
    # Never checked is not the same as connected. Assuming a free DM would land
    # is exactly the assumption that gets an account flagged.
    assert routing.route_for(status()) is None


def test_allowed_routes_for_a_connection_are_just_the_dm():
    assert routing.allowed(status(connection=True)) == (provider.DM,)


def test_allowed_routes_for_a_stranger_are_the_three_fallbacks():
    assert routing.allowed(status(connection=False, inmail=False)) == provider.FALLBACK_ROUTES


def test_paid_inmail_drops_out_of_the_fallbacks_with_no_credits():
    assert routing.allowed(status(connection=False, inmail=False, credits=0)) == (
        provider.INVITE_NOTE,
        provider.INVITE_THEN_DM,
    )
