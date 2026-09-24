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

# \w is Unicode-aware in Python's re by default, so it accepts a Turkish "ç" or
# a Czech "á" alongside ASCII letters/digits/underscore - needed now that
# `UnipileProvider._identifier` hands this a percent-decoded slug rather than
# the raw (still-ASCII) URL path segment. The hyphen is added explicitly since
# `\w` doesn't include it and real slugs are hyphen-separated. What stays
# excluded, and why:
#   `.` and `/` (and `\`)  - how a path-traversal payload escapes this segment
#                            once it's no longer percent-encoded (`..`, `../`).
#   `%`                    - re-admitting it would let a caller hand this regex
#                            an already-percent-encoded (or double-encoded)
#                            string that `quote(..., safe="")` would then encode
#                            a second time, corrupting the request.
#   whitespace / controls  - not part of `\w`; a raw slug has no business
#                            carrying either.
_IDENTIFIER_RE = re.compile(r"^[\w-]{1,128}$")

# `apiNN.unipile.com:15169` - a dotted hostname, optionally with a port, and
# nothing else. The dot is what separates a real DSN from a typo: a bare word
# like "not-a-url" is a syntactically valid single-label host, so without it a
# mistyped DSN would be silently promoted to a request target. Unipile is never
# a single-label host, so requiring the dot costs nothing real.
_BARE_HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$")


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
        # Unipile's dashboard shows the DSN as a bare `apiNN.unipile.com:PORT`,
        # so a scheme-less value is what a correct setup actually produces.
        # Assume https rather than refusing it: this can only ever upgrade the
        # connection, and an explicit `http://` is still rejected below.
        if "://" not in dsn and _BARE_HOST.match(dsn):
            dsn = f"https://{dsn}"
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

    def _resolve_parameter(self, kind: str, name: str) -> str | None:
        """LinkedIn's own id for a name of the given kind, or None.

        Never raises: an id that cannot be looked up must cost a filter, not
        the search - an unfiltered result set is still useful, an exception is
        not.
        """
        if not name.strip():
            return None
        try:
            payload = self._call(
                "GET", "/api/v1/linkedin/search/parameters",
                params={"account_id": self.account_id, "type": kind, "keywords": name},
            )
        except UnipileError:
            return None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return None
        first = items[0]
        identifier = first.get("id") if isinstance(first, dict) else None
        return str(identifier) if identifier else None

    def resolve_company(self, name: str) -> str | None:
        """LinkedIn's id for a company page, or None if the name matches none."""
        return self._resolve_parameter("COMPANY", name)

    def resolve_location(self, name: str) -> str | None:
        """LinkedIn's own id for a place name, or None if it cannot be placed.

        The search takes ids, not names. Never raises: a location that cannot
        be resolved must cost the filter, not the search - an unfiltered result
        set is still useful, an exception here is not.
        """
        return self._resolve_parameter("LOCATION", name)

    def search_people(
        self,
        company: str,
        keywords: list[str],
        *,
        limit: int = 5,
        location: str | None = None,
    ) -> list[dict[str, Any]]:
        """People matching a company name, optionally narrowed to one place.

        `account_id` is a query parameter here, not a body field. Sent in the
        body it is a hard 400 ("path": "/account_id", "Required property"),
        which is what this method did until it was first run against a live
        account - the company-search path had never returned anything.

        `location` is a place *name*; it is resolved to LinkedIn's id and
        dropped if it cannot be. It is worth the extra call: searching a
        company name alone returns same-named companies worldwide, and the
        client's own country and state are known for every Upwork posting.
        Industry is deliberately not filtered on - measured, it removed every
        genuine match, because it keys off how LinkedIn classifies a person's
        employer and small companies are classified thinly or not at all.
        """
        # A company id is an exact filter; a company name is a guess at free
        # text. Measured live, the difference is not marginal: searching the
        # name "Karma and Luck" returned a therapist, an esthetician and a high
        # school student, while its company id returned the founder and CEO,
        # the ecommerce operations manager, and the store managers.
        #
        # The role words travel only inside a company filter, where they rank
        # within that company's people. Without one they compete with the
        # company name and win: "Nexora founder OR CTO OR recruiter" returned
        # six co-founders of six unrelated companies and nobody at Nexora,
        # because LinkedIn matched the common words and lost the rare name.
        body: dict[str, Any] = {"api": "classic", "category": "people"}
        company_id = self.resolve_company(company)
        if company_id:
            body["company"] = [company_id]
            if keywords:
                body["keywords"] = " OR ".join(keywords)
        else:
            body["keywords"] = company.strip()
        if location:
            location_id = self.resolve_location(location)
            if location_id:
                body["location"] = [location_id]
        payload = self._call(
            "POST", "/api/v1/linkedin/search",
            params={"account_id": self.account_id}, json=body,
        )
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
