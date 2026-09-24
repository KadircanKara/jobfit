"""A provider that records what a send would have been, and sends nothing.

This is the only provider in phase 1. It answers status from the contact row so
the UI can drive every branch of the routing tree by hand, and it marks every
reference `stub:` so a database read months from now cannot be misread as
evidence that a message went out.
"""
from __future__ import annotations

import logging
import uuid

from jobhunt.config import Config
from jobhunt.db.models import Contact
from jobhunt.outreach import provider

log = logging.getLogger(__name__)


class StubProvider:
    """Implements LinkedInProvider without a network."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # Contact ids the UI or a test has marked as having accepted an invite.
        # In-process on purpose: it is a fixture for the poller, not a fact
        # about the world, and it must not outlive the run that set it.
        self._accepted: set[int] = set()

    def status(self, contact: Contact) -> provider.ContactStatus:
        return provider.ContactStatus(
            is_connection=contact.is_connection,
            can_send_inmail=contact.can_send_inmail,
            inmail_credits=self.credits(),
        )

    def credits(self) -> int:
        return int(self.config.get("outreach", "inmail_credits", default=0))

    def send_dm(self, contact: Contact, body: str) -> provider.SendResult:
        return self._record("dm", contact, body)

    def send_inmail(self, contact: Contact, body: str, *, paid: bool) -> provider.SendResult:
        return self._record("paid_inmail" if paid else "free_inmail", contact, body)

    def send_invite(self, contact: Contact, note: str | None) -> provider.SendResult:
        return self._record("invite", contact, note or "")

    def invite_accepted(self, contact: Contact) -> bool:
        return contact.id in self._accepted

    def accept(self, contact_id: int) -> None:
        """Mark an invite accepted. The seam the poller is exercised through."""
        self._accepted.add(contact_id)

    def _record(self, kind: str, contact: Contact, body: str) -> provider.SendResult:
        ref = f"stub:{uuid.uuid4().hex[:12]}"
        log.info("outreach stub %s to %s (%d chars) ref=%s", kind, contact.full_name, len(body), ref)
        return provider.SendResult(ok=True, ref=ref)
