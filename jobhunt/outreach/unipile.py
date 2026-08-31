"""The provider that actually sends.

Everything a message needs to be correct — the route, the cap, the approval — was
decided before this file is reached. Its only job is to turn that decision into a
call, and to refuse rather than guess when the recipient cannot be addressed.

A send here is real, immediate, and cannot be unsent. That is why `build_sender`
defaults to the stub: reaching this code has to be a choice someone made on
purpose.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from jobhunt.config import Config
from jobhunt.db.models import Contact
from jobhunt.outreach import provider, stub
from jobhunt.outreach.service import normalize_profile_url
from jobhunt.outreach.unipile_client import UnipileClient, UnipileError, credentials_from_env

log = logging.getLogger(__name__)

_PROFILE_MARKER = "linkedin.com/in/"


class UnipileProvider:
    """Implements LinkedInProvider against a live account."""

    def __init__(self, config: Config, client: UnipileClient) -> None:
        self.config = config
        self.client = client

    def status(self, contact: Contact) -> provider.ContactStatus:
        identifier = self._identifier(contact)
        if identifier is None:
            return provider.ContactStatus()
        try:
            user = self.client.get_user(identifier)
        except UnipileError:
            log.warning("unipile status lookup failed for %s", contact.full_name)
            return provider.ContactStatus()
        if not isinstance(user, dict):
            # The client only promises JSON-shaped output, not this shape. A body
            # that parsed but isn't a dict must degrade to unknown, not raise
            # AttributeError out of a read that callers treat as safe.
            log.warning("unipile status lookup returned an unexpected shape for %s", contact.full_name)
            return provider.ContactStatus()
        self._remember(contact, user)
        distance = str(user.get("network_distance") or "").upper()
        return provider.ContactStatus(
            is_connection=distance in ("FIRST_DEGREE", "DISTANCE_1"),
            # LinkedIn does not state InMail eligibility per person on this read,
            # so it stays unknown rather than being asserted wrongly.
            can_send_inmail=None,
            inmail_credits=self.credits(),
        )

    def credits(self) -> int:
        """The credit count is declared in config, not observed from the account.

        Unipile has no endpoint this client calls to read remaining InMail
        credits, so the number the caps and routing logic budget against is
        whatever the user typed into config - a ceiling this provider trusts,
        not a live balance it has verified.
        """
        return int(self.config.get("outreach", "inmail_credits", default=0))

    def send_dm(self, contact: Contact, body: str) -> provider.SendResult:
        return self._send(contact, lambda pid: self.client.start_chat(pid, body), "message_id")

    def send_inmail(self, contact: Contact, body: str, *, paid: bool) -> provider.SendResult:
        """`paid` never reaches the call: both routes are the same InMail send.

        The distinction is spent before this method is reached - it decided
        which budget the caller charged the message against - and Unipile's
        `start_chat` has no parameter that tells it which credit paid for the
        send. Threading `paid` through anyway keeps this method's signature
        matching the protocol every other provider implements.
        """
        return self._send(
            contact, lambda pid: self.client.start_chat(pid, body, inmail=True), "message_id"
        )

    def send_invite(self, contact: Contact, note: str | None) -> provider.SendResult:
        return self._send(contact, lambda pid: self.client.send_invite(pid, note), "invitation_id")

    def invite_accepted(self, contact: Contact) -> bool:
        return bool(self.status(contact).is_connection)

    def _send(
        self, contact: Contact, call: Callable[[str], dict[str, Any]], ref_key: str
    ) -> provider.SendResult:
        provider_id = self._provider_id(contact)
        if provider_id is None:
            # Refused before anything moves: a half-sent outreach is worse than
            # an unsent one, because only the unsent one can be retried.
            return provider.SendResult(
                ok=False, failure=f"{contact.full_name} has no LinkedIn identifier to send to."
            )
        try:
            payload = call(provider_id)
        except UnipileError as exc:
            return provider.SendResult(ok=False, failure=str(exc))
        if not isinstance(payload, dict):
            # A 2xx with the wrong body shape is not a `UnipileError`, but by the
            # time it's seen here the message has already left the account - the
            # one thing this branch must never do is raise and let the caller
            # believe nothing happened, because a retry would send it twice.
            return provider.SendResult(ok=False, failure=f"unipile returned an unexpected body: {payload!r}")
        return provider.SendResult(ok=True, ref=str(payload.get(ref_key) or "") or None)

    def _provider_id(self, contact: Contact) -> str | None:
        if contact.provider_id:
            return contact.provider_id
        identifier = self._identifier(contact)
        if identifier is None:
            return None
        try:
            user = self.client.get_user(identifier)
        except UnipileError:
            return None
        self._remember(contact, user)
        return contact.provider_id

    @staticmethod
    def _identifier(contact: Contact) -> str | None:
        """The value `get_user` will accept: the stored id, or a slug pulled from the URL.

        Reuses `normalize_profile_url` rather than re-parsing the URL, so there is
        exactly one place that knows what a LinkedIn profile URL looks like.
        """
        if contact.provider_id:
            return contact.provider_id
        normalized = normalize_profile_url(contact.profile_url)
        if normalized is None or _PROFILE_MARKER not in normalized:
            return None
        # Only the first path segment after `/in/` is the slug. A profile URL
        # copied from the activity feed or the contact-info panel carries more
        # of the path (`.../recent-activity/all/`), which would otherwise be
        # handed to `get_user` as though it were part of the identifier.
        remainder = normalized.split(_PROFILE_MARKER, 1)[1]
        return remainder.split("/", 1)[0] or None

    @staticmethod
    def _remember(contact: Contact, user: dict) -> None:
        """Resolving costs a call; a stored id means it happens once per person.

        Deliberately not undone if the send that follows fails: the id names who
        this person is on LinkedIn, not whether the last message to them landed,
        so it stays true regardless of the send's outcome.
        """
        found = user.get("provider_id")
        if found and not contact.provider_id:
            contact.provider_id = str(found)


def build_sender(config: Config) -> provider.LinkedInProvider:
    """Which provider this process sends through. Stub unless told otherwise."""
    choice = str(config.get("outreach", "provider", default="stub")).strip().lower()
    if choice != "unipile":
        return stub.StubProvider(config)
    dsn, api_key, account_id = credentials_from_env()
    return UnipileProvider(config, UnipileClient(dsn, api_key, account_id))
