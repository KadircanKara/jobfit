"""Strategies B and E: the yc-oss company list plus domain-guessed tokens.

Measured on 2026-08-20 (references/sources.md): 10 of a random 30 hiring
companies resolve to a live board from a token guessed as "website domain minus
TLD", tried against ashby, greenhouse, then lever. That is the cold start, and
it needs no company name from the user.

This module only writes candidates. It never fetches a board itself: the ordinary
sync validation loop proves them out one request each and kills a wrong guess on
its first failure. Keeping every board fetch in one place is what makes the
per-run cap and the rate limits mean anything.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from collections import Counter

import httpx
from sqlalchemy import func, select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board, Company, utcnow
from jobhunt.db.session import session_scope
from jobhunt.discovery import patterns

HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"

# Ordered by measured hit rate on the YC cohort, best first. Only providers with
# an adapter are worth guessing: a candidate nothing can fetch never validates
# and just sits in the table.
GUESS_PROVIDERS = ("ashby", "greenhouse", "lever")


@dataclasses.dataclass
class SeedResult:
    companies: int = 0
    with_website: int = 0
    tokens_guessed: int = 0
    new_companies: int = 0
    new_boards: int = 0
    known_boards: int = 0
    raw_path: str | None = None
    by_provider: dict[str, int] = dataclasses.field(default_factory=dict)

    def summary(self) -> str:
        providers = " ".join(f"{k}={v}" for k, v in sorted(self.by_provider.items())) or "-"
        return (
            f"yc: companies={self.companies} with_website={self.with_website} "
            f"guessed={self.tokens_guessed} new_companies={self.new_companies} "
            f"new_boards={self.new_boards} known={self.known_boards} [{providers}]"
        )


def fetch_hiring(config: Config, run_key: str) -> pathlib.Path:
    """Pass one: persist the list. A parser bug must never cost this request."""
    out_dir = config.raw_dir / "yc" / run_key
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "hiring.json"
    headers = {"User-Agent": config.get("http", "user_agent"), "Accept": "application/json"}
    timeout = float(config.get("http", "timeout_seconds", default=30.0))
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        response = client.get(HIRING_URL)
        response.raise_for_status()
        payload = response.json()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def latest_raw(config: Config) -> pathlib.Path | None:
    root = config.raw_dir / "yc"
    if not root.exists():
        return None
    runs = sorted(root.glob("*/hiring.json"))
    return runs[-1] if runs else None


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = website.strip().removeprefix("https://").removeprefix("http://")
    host = host.split("/")[0].split("?")[0].lower().removeprefix("www.")
    return host if "." in host else None


def seed(
    config: Config,
    raw_path: pathlib.Path,
    limit: int | None = None,
    dry_run: bool = False,
    providers: tuple[str, ...] = GUESS_PROVIDERS,
) -> SeedResult:
    """Pass two: company list -> company rows plus candidate boards."""
    companies = json.loads(raw_path.read_text(encoding="utf-8"))
    result = SeedResult(companies=len(companies), raw_path=str(raw_path))
    new_by_provider: Counter[str] = Counter()

    with session_scope(config.db_path) as session:
        known: set[tuple[str, str]] = {
            (provider, token)
            for provider, token in session.execute(select(Board.provider, Board.token)).all()
        }
        seeded = 0
        for entry in companies:
            if limit and seeded >= limit:
                break
            domain = domain_of(entry.get("website"))
            if not domain:
                continue
            result.with_website += 1
            token = patterns.guess_token(domain)
            if not token:
                continue

            company = None
            if not dry_run:
                company, created = _company_row(session, entry, domain)
                result.new_companies += int(created)
            result.tokens_guessed += 1
            seeded += 1

            for provider in providers:
                key = (provider, token)
                if key in known:
                    result.known_boards += 1
                    continue
                known.add(key)
                new_by_provider[provider] += 1
                result.new_boards += 1
                if dry_run:
                    continue
                session.add(
                    Board(
                        provider=provider,
                        token=token,
                        company_id=company.id if company else None,
                        discovered_via="yc",
                        discovered_at=utcnow(),
                        market="global_remote",
                        status="candidate",
                        # NULL means due now. The next sync validates it, and a
                        # wrong guess dies on that single request.
                        next_fetch_at=None,
                        notes=f"guessed from {domain}",
                    )
                )

    result.by_provider = dict(new_by_provider)
    return result


def _company_row(session, entry: dict, domain: str):
    """Create or upgrade the company row. Returns (company, created).

    yc metadata is worth keeping: batch and team size are ranking inputs in
    PLAN.md section 7, and they arrive free with the list.
    """
    name = entry.get("name") or domain
    before = session.scalar(select(func.count(Company.id)))
    company = store.get_or_create_company(session, name, domain)
    company.yc_batch = entry.get("batch") or company.yc_batch
    company.yc_slug = entry.get("slug") or company.yc_slug
    company.yc_tags = entry.get("industries") or company.yc_tags
    company.team_size = entry.get("team_size") or company.team_size
    session.flush()
    created = session.scalar(select(func.count(Company.id))) > before
    return company, created


def run(
    config: Config,
    limit: int | None = None,
    dry_run: bool = False,
    from_raw: bool = False,
) -> SeedResult:
    """Fetch the list unless --from-raw, then seed. Returns a one-line summary."""
    path = latest_raw(config) if from_raw else None
    if path is None:
        if from_raw:
            raise FileNotFoundError("no stored yc payload. run without --from-raw once.")
        path = fetch_hiring(config, utcnow().strftime("%Y%m%dT%H%M%S"))
    return seed(config, path, limit=limit, dry_run=dry_run)
