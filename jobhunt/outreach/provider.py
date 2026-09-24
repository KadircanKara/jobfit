"""The seam between outreach logic and whatever actually talks to LinkedIn.

Everything above this file works in terms of a status and a route. Only the
provider knows about accounts, credits, and the network — which is why the whole
of phase 1 can run against a stub without a single conditional elsewhere.
"""
from __future__ import annotations

import dataclasses
from typing import Protocol

from jobhunt.db.models import Contact

DM = "dm"
FREE_INMAIL = "free_inmail"
INVITE_NOTE = "invite_note"
INVITE_THEN_DM = "invite_then_dm"
PAID_INMAIL = "paid_inmail"

# The three the user picks between when no free route exists. Ordered cheapest
# commitment first, so the option that spends a credit is never the default read.
FALLBACK_ROUTES = (INVITE_NOTE, INVITE_THEN_DM, PAID_INMAIL)

ALL_ROUTES = (DM, FREE_INMAIL, *FALLBACK_ROUTES)


@dataclasses.dataclass(frozen=True)
class ContactStatus:
    """What LinkedIn allows for one person right now.

    `None` on either flag means never checked, which the tree treats as the
    conservative answer rather than a negative.
    """

    is_connection: bool | None = None
    can_send_inmail: bool | None = None
    inmail_credits: int = 0


@dataclasses.dataclass(frozen=True)
class SendResult:
    ok: bool
    ref: str | None = None
    failure: str | None = None


class LinkedInProvider(Protocol):
    def status(self, contact: Contact) -> ContactStatus: ...

    def credits(self) -> int: ...

    def send_dm(self, contact: Contact, body: str) -> SendResult: ...

    def send_inmail(self, contact: Contact, body: str, *, paid: bool) -> SendResult: ...

    def send_invite(self, contact: Contact, note: str | None) -> SendResult: ...

    def invite_accepted(self, contact: Contact) -> bool: ...
