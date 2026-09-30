"""Fetch a posting's own page when the source gave no description.

Some boards list jobs without their body (Personio's feed carries none), and
the fit gate cannot judge fit or read a work-authorization requirement from a
title. Before a job is gated, its page is fetched and flattened to text here.

Only the app fetches. The gate itself never gets a network tool, so a posting
cannot steer a model into browsing somewhere.

The URL comes from scraped data, so it is treated as untrusted: http(s) only,
and every hop of a redirect must resolve to a public address. Without that a
posting could point the server at its own loopback or a private network.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse

import httpx

from jobhunt.config import Config
from jobhunt.db.models import Job, utcnow
from jobhunt.db.session import session_scope
from jobhunt.pipeline import normalize as norm

TIMEOUT = 10.0
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
# LinkedIn has its own detail fetch behind a rate guard; a second, unguarded
# path to the same pages would be the fastest way to get the account blocked.
SKIP_SOURCES = frozenset({"linkedin"})


class Unsafe(ValueError):
    """A URL this module will not fetch."""


def fill(config: Config, job_ids: list[int], client: httpx.Client | None = None) -> int:
    """Fetch and store descriptions for these jobs. Returns how many were filled.

    A page that fails, or yields too little text to count as a description, is
    left as it was: the job is still gated, on what it has.
    """
    targets: list[tuple[int, str]] = []
    with session_scope(config.db_path) as session:
        for job_id in job_ids:
            job = session.get(Job, job_id)
            if job is None or job.source in SKIP_SOURCES or job.jd_completeness == "full":
                continue
            url = job.source_url or job.apply_url
            if url:
                targets.append((job_id, url))
    if not targets:
        return 0

    own = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": config.get("http", "user_agent"), "Accept": "text/html"},
    )
    filled = 0
    try:
        for job_id, url in targets:
            try:
                html = _get(client, url)
            except (httpx.HTTPError, Unsafe, UnicodeDecodeError):
                continue
            text = norm.html_to_text(html)
            if norm.completeness(text) != "full":
                continue
            with session_scope(config.db_path) as session:
                job = session.get(Job, job_id)
                if job is None:
                    continue
                job.description_text = text
                job.description_md = norm.html_to_markdown(html)
                job.jd_completeness = "full"
                job.jd_source = "page"
                job.jd_extracted_at = utcnow()
            filled += 1
    finally:
        if own:
            client.close()
    return filled


def _get(client: httpx.Client, url: str) -> str:
    """The page body, following at most a few redirects, each one checked."""
    for _hop in range(MAX_REDIRECTS + 1):
        check(url)
        with client.stream("GET", url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise Unsafe("redirect without a location")
                url = urllib.parse.urljoin(url, location)
                continue
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "html"):
                raise Unsafe("not an html page")
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise Unsafe("page too large")
            return body.decode(response.encoding or "utf-8", errors="replace")
    raise Unsafe("too many redirects")


def check(url: str) -> None:
    """Refuse anything but http(s) to a host that resolves only to public addresses."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unsafe(f"not a web address: {url!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Unsafe(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise Unsafe(f"{parts.hostname} resolves to a non-public address")
