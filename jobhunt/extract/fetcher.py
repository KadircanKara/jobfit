"""Detail-page fetching for the ladder, with the caching PLAN.md 9.5 requires.

Raw HTML is written to data/raw/detail/{source}/{job_id}.html before anything
parses it, so fixing an extractor never costs a request against a fragile
source. Same fetch/parse split as sync, same reason.
"""
from __future__ import annotations

import pathlib

import httpx

from jobhunt.config import Config


def cache_path(config: Config, source: str, job_id: int) -> pathlib.Path:
    return config.raw_dir / "detail" / source / f"{job_id}.html"


def fetch(
    config: Config, source: str, job_id: int, url: str, force: bool = False
) -> tuple[str | None, str]:
    """Return (html, origin) where origin is 'cache', 'network', or 'error:...'.

    One request, no retries, no crawling. This runs for a job that already
    survived filtering, usually one the user is about to apply to.
    """
    path = cache_path(config, source, job_id)
    if path.exists() and not force:
        return path.read_text(encoding="utf-8", errors="replace"), "cache"

    headers = {
        "User-Agent": config.get("http", "user_agent"),
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = float(config.get("http", "timeout_seconds", default=30.0))
    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError as exc:
        return None, f"error:{type(exc).__name__}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return html, "network"
