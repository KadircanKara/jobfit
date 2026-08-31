"""HTTP against Unipile, and nothing else.

Kept free of Contact, routes, and caps so the provider above it can be tested
without a transport, and so the one place that holds an API key is small enough
to audit at a glance. The API key travels only in a request header and must
never surface in an exception, log, or return value — errors below carry the
response body, never the request that made the call.
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")


class UnipileError(Exception):
    """A call Unipile refused, or one that never reached it. Never carries the API key."""


class MissingCredentials(Exception):
    """Unipile is the configured provider but its environment variables are unset."""


ENV_VARS = ("UNIPILE_DSN", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID")


def credentials_from_env() -> tuple[str, str, str]:
    """Read Unipile credentials from the environment only — never from config files."""
    values = [os.environ.get(name, "").strip() for name in ENV_VARS]
    missing = [name for name, value in zip(ENV_VARS, values, strict=True) if not value]
    if missing:
        raise MissingCredentials(
            "outreach.provider is 'unipile' but these are unset: " + ", ".join(missing)
        )
    return values[0], values[1], values[2]


class UnipileClient:
    """A thin wrapper over Unipile's REST API for the calls outreach needs."""

    def __init__(
        self,
        dsn: str,
        api_key: str,
        account_id: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        dsn = dsn.rstrip("/")
        parsed = urlsplit(dsn)
        if parsed.scheme != "https" or not parsed.hostname:
            # An http:// DSN would ship the API key in cleartext; a malformed one
            # would raise httpx.InvalidURL deep inside a request. Reject both here,
            # at construction, rather than partway through a call.
            raise UnipileError(f"UNIPILE_DSN must be an https URL with a host, got: {dsn!r}")
        self.dsn = dsn
        self.account_id = account_id
        self._key = api_key
        # follow_redirects stays False (httpx's default): a redirect would carry
        # the X-API-KEY header to whatever host it names, including one Unipile
        # itself never chose. Do not turn this on.
        self._client = client or httpx.Client(timeout=30.0)

    def get_user(self, identifier: str) -> dict[str, Any]:
        if not isinstance(identifier, str) or not _IDENTIFIER_RE.match(identifier):
            raise UnipileError("get_user: identifier has an unexpected shape")
        segment = quote(identifier, safe="")
        return self._call("GET", f"/api/v1/users/{segment}", params={"account_id": self.account_id})

    def start_chat(self, provider_id: str, text: str, *, inmail: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "account_id": self.account_id,
            "attendees_ids": [provider_id],
            "text": text,
        }
        if inmail:
            body["linkedin"] = {"api": "classic", "inmail": True}
        return self._call("POST", "/api/v1/chats", json=body)

    def send_invite(self, provider_id: str, note: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"account_id": self.account_id, "provider_id": provider_id}
        if note:
            body["message"] = note
        return self._call("POST", "/api/v1/users/invite", json=body)

    def search_people(self, company: str, keywords: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        # Unverified without live credentials: shape per Unipile's docs, to be
        # corrected in manual testing if it's wrong.
        body = {
            "account_id": self.account_id,
            "api": "classic",
            "category": "people",
            "keywords": f"{company} {' OR '.join(keywords)}",
        }
        payload = self._call("POST", "/api/v1/linkedin/search", json=body)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise UnipileError("search_people: response body was not the expected shape")
        return items[:limit]

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(
                method,
                f"{self.dsn}{path}",
                headers={"X-API-KEY": self._key, "accept": "application/json"},
                **kwargs,
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Never str(exc) here without checking: httpx exceptions can echo the
            # request, which carries the key header. The class name alone is safe.
            raise UnipileError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            # Body only, never the request: the request carries the key. Bound
            # the raw bytes before decoding so an oversized body isn't fully
            # materialised as text first.
            body = response.content[:300].decode("utf-8", errors="replace")
            raise UnipileError(f"{method} {path} returned {response.status_code}: {body}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            # A 2xx with a malformed body is still an httpx-independent failure;
            # keep the single-error-type contract this module promises.
            raise UnipileError(f"{method} {path} returned a non-JSON body") from exc
