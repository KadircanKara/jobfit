"""HTTP against Unipile, and nothing else.

Kept free of Contact, routes, and caps so the provider above it can be tested
without a transport, and so the one place that holds an API key is small enough
to audit at a glance. The API key travels only in a request header and must
never surface in an exception, log, or return value — errors below carry the
response body, never the request that made the call.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


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
        self.dsn = dsn.rstrip("/")
        self.account_id = account_id
        self._key = api_key
        self._client = client or httpx.Client(timeout=30.0)

    def get_user(self, identifier: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/users/{identifier}", params={"account_id": self.account_id})

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
        items = payload.get("items") or []
        return items[:limit]

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(
                method,
                f"{self.dsn}{path}",
                headers={"X-API-KEY": self._key, "accept": "application/json"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            # Never str(exc) here without checking: httpx exceptions can echo the
            # request, which carries the key header. The class name alone is safe.
            raise UnipileError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            # Body only, never the request: the request carries the key.
            raise UnipileError(f"{method} {path} returned {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}
