"""Strategy C: bulk board backfill from the Common Crawl URL index.

Common Crawl publishes a free, queryable CDX index of every URL it has crawled.
One request per provider host returns thousands of historical ATS URLs, and the
pattern bank turns those into board tokens. Measured on CC-MAIN-2026-30:

    boards.greenhouse.io/*   12075 records ->  1790 tokens
    jobs.ashbyhq.com/*       13865 records ->  1833 tokens
    *.recruitee.com           7128 records ->   707 tokens
    *.jobs.personio.de        5429 records ->   668 tokens

This is a backfill, not a daily job. Run it at setup and maybe quarterly.

Two constraints that shape the code:

1. **The data is stale.** Many tokens are dead boards from companies that folded
   or switched ATS. Everything here enters as a candidate and is proven by the
   ordinary sync validation loop, one request each, dead on first failure.
2. **Do not overload the index server.** Common Crawl asks this explicitly. One
   request per provider per run, paged, with the source's own delay between
   pages, and a hard page cap.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import time
from collections import Counter

import httpx
from sqlalchemy import select

from jobhunt.config import Config
from jobhunt.db.models import Board, utcnow
from jobhunt.db.session import session_scope
from jobhunt.discovery import patterns

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# The URL pattern to ask the index for, per provider. Path-keyed providers use a
# path prefix; subdomain-keyed providers use a subdomain wildcard, which the CDX
# API supports natively.
PROVIDER_PATTERNS: dict[str, str] = {
    "greenhouse": "boards.greenhouse.io/*",
    "ashby": "jobs.ashbyhq.com/*",
    "lever": "jobs.lever.co/*",
    "recruitee": "*.recruitee.com",
    "personio": "*.jobs.personio.de",
    "workable": "apply.workable.com/*",
    "smartrecruiters": "careers.smartrecruiters.com/*",
}

DEFAULT_PROVIDERS = ("greenhouse", "ashby", "recruitee", "personio", "workable")
MAX_PAGES = 5
PAGE_DELAY_SECONDS = 2.0
RETRIES = 3


@dataclasses.dataclass
class BackfillResult:
    index_id: str = ""
    providers: int = 0
    records: int = 0
    tokens: int = 0
    new_boards: int = 0
    known_boards: int = 0
    failures: list[str] = dataclasses.field(default_factory=list)
    by_provider: dict[str, int] = dataclasses.field(default_factory=dict)

    def summary(self) -> str:
        providers = " ".join(f"{k}={v}" for k, v in sorted(self.by_provider.items())) or "-"
        failed = f" failed={','.join(self.failures)}" if self.failures else ""
        return (
            f"commoncrawl: index={self.index_id} providers={self.providers} "
            f"records={self.records} tokens={self.tokens} new_boards={self.new_boards} "
            f"known={self.known_boards}{failed} [{providers}]"
        )


def latest_index(client: httpx.Client) -> str:
    """The newest monthly index id, e.g. CC-MAIN-2026-30."""
    response = client.get(COLLINFO_URL)
    response.raise_for_status()
    collections = response.json()
    if not collections:
        raise RuntimeError("commoncrawl: collinfo.json returned no indices")
    return collections[0]["id"]


def _query(client: httpx.Client, index_id: str, pattern: str, page: int) -> list[str]:
    """One CDX page of URLs. Retries, because the index 502s under load."""
    url = f"https://index.commoncrawl.org/{index_id}-index"
    params = {"url": pattern, "output": "json", "fl": "url", "page": str(page)}
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
        else:
            if response.status_code == 200:
                return [line for line in response.text.splitlines() if line.strip()]
            # 404 means this pattern has no more pages, which is a normal end.
            if response.status_code == 404:
                return []
            last = RuntimeError(f"HTTP {response.status_code}")
        if attempt < RETRIES - 1:
            time.sleep(PAGE_DELAY_SECONDS * (attempt + 1))
    raise RuntimeError(f"commoncrawl: {pattern} failed after {RETRIES} attempts: {last}")


def fetch_provider(
    config: Config,
    client: httpx.Client,
    index_id: str,
    provider: str,
    run_key: str,
    max_pages: int = MAX_PAGES,
) -> pathlib.Path:
    """Pass one: persist the raw URL list. Re-parsing must never re-query the index."""
    pattern = PROVIDER_PATTERNS[provider]
    out_dir = config.raw_dir / "commoncrawl" / run_key
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{provider}.jsonl"

    with path.open("w", encoding="utf-8") as handle:
        for page in range(max_pages):
            lines = _query(client, index_id, pattern, page)
            if not lines:
                break
            handle.write("\n".join(lines) + "\n")
            if page < max_pages - 1:
                time.sleep(PAGE_DELAY_SECONDS)
    return path


def parse_file(path: pathlib.Path) -> tuple[int, set[patterns.BoardHit]]:
    """Pass two: stored URL list -> board hits. Pure, no network, no database."""
    hits: set[patterns.BoardHit] = set()
    records = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records += 1
            try:
                url = json.loads(line).get("url")
            except json.JSONDecodeError:
                continue
            found, _ = patterns.scan(url)
            hits |= found
    return records, hits


def store_hits(
    config: Config, provider: str, hits: set[patterns.BoardHit], market: str, dry_run: bool
) -> tuple[int, int]:
    """Write candidates. Returns (new, known)."""
    new = known = 0
    with session_scope(config.db_path) as session:
        existing: set[str] = {
            token
            for (token,) in session.execute(
                select(Board.token).where(Board.provider == provider)
            ).all()
        }
        for hit in sorted(hits):
            if hit.provider != provider:
                continue
            if hit.token in existing:
                known += 1
                continue
            existing.add(hit.token)
            new += 1
            if dry_run:
                continue
            session.add(
                Board(
                    provider=hit.provider,
                    token=hit.token,
                    discovered_via="commoncrawl",
                    discovered_at=utcnow(),
                    market=market,
                    status="candidate",
                    next_fetch_at=None,
                    notes="common crawl backfill, unvalidated",
                )
            )
    return new, known


def run(
    config: Config,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    market: str = "global_remote",
    max_pages: int = MAX_PAGES,
    dry_run: bool = False,
    from_raw: str | None = None,
) -> BackfillResult:
    """Fetch each provider's URL list, then turn it into candidate boards.

    A provider that fails is recorded and skipped. One flaky index query must not
    cost the other five providers their backfill.
    """
    result = BackfillResult()
    counts: Counter[str] = Counter()
    unknown = [p for p in providers if p not in PROVIDER_PATTERNS]
    if unknown:
        raise KeyError(f"no common crawl pattern for {', '.join(unknown)}")

    headers = {"User-Agent": config.get("http", "user_agent"), "Accept": "application/json"}
    timeout = float(config.get("http", "timeout_seconds", default=30.0))

    if from_raw:
        run_key = from_raw
        result.index_id = f"stored:{from_raw}"
        paths = {p: config.raw_dir / "commoncrawl" / run_key / f"{p}.jsonl" for p in providers}
    else:
        run_key = utcnow().strftime("%Y%m%dT%H%M%S")
        paths = {}
        # A long timeout: a full CDX page is over a megabyte of JSON lines.
        with httpx.Client(timeout=max(timeout, 300.0), headers=headers, follow_redirects=True) as client:
            result.index_id = latest_index(client)
            for provider in providers:
                try:
                    paths[provider] = fetch_provider(
                        config, client, result.index_id, provider, run_key, max_pages
                    )
                except Exception as exc:  # noqa: BLE001 - one provider must not sink the rest
                    result.failures.append(f"{provider}:{type(exc).__name__}")

    for provider, path in paths.items():
        if not path.exists():
            result.failures.append(f"{provider}:missing")
            continue
        records, hits = parse_file(path)
        provider_hits = {h for h in hits if h.provider == provider}
        result.records += records
        result.tokens += len(provider_hits)
        new, known = store_hits(config, provider, provider_hits, market, dry_run)
        result.new_boards += new
        result.known_boards += known
        counts[provider] = new
        result.providers += 1

    result.by_provider = dict(counts)
    return result
