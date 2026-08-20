"""Strategy A: turn URLs the pipeline already stored into fetchable boards.

This is the flywheel. Every ingested job carries an apply_url, a source_url, and
a description body. A large share of them leak an ATS token. Each token unlocks
the company's entire board, not the one job the aggregator carried, and those
boards' own URLs surface more tokens. Discovery becomes a byproduct of ingestion
instead of a prerequisite for it, which is the whole point of PLAN.md 3.5.

Harvesting is incremental by default: it remembers the highest job id it has
scanned and only looks at rows above it. `--full` rescans everything, which is
what to run after changing the pattern bank.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from collections import Counter

from sqlalchemy import select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board, Company, Job
from jobhunt.db.session import session_scope
from jobhunt.discovery import patterns

WATERMARK_KEY = "harvest.last_job_id"

# Providers with no adapter yet still get a row. The board table is a record of
# what exists, not of what this version happens to be able to fetch, and a
# provider that lands later inherits a ready-made backlog.
UNFETCHABLE_NOTE = "no adapter yet"


@dataclasses.dataclass
class HarvestResult:
    scanned: int = 0
    hits: int = 0
    hints: int = 0
    new_boards: int = 0
    guessed_boards: int = 0
    known_boards: int = 0
    companies_linked: int = 0
    domains_learned: int = 0
    high_water: int = 0
    by_provider: dict[str, int] = dataclasses.field(default_factory=dict)

    def summary(self) -> str:
        providers = " ".join(f"{k}={v}" for k, v in sorted(self.by_provider.items())) or "-"
        return (
            f"harvest: scanned={self.scanned} hits={self.hits} hints={self.hints} "
            f"new_boards={self.new_boards} guessed={self.guessed_boards} "
            f"known={self.known_boards} companies_linked={self.companies_linked} "
            f"domains_learned={self.domains_learned} high_water={self.high_water} "
            f"[{providers}]"
        )


def harvest(
    config: Config,
    full: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    guess_from_hints: bool = True,
) -> HarvestResult:
    """Scan stored jobs for ATS tokens and write candidate boards."""
    result = HarvestResult()
    new_by_provider: Counter[str] = Counter()

    with session_scope(config.db_path) as session:
        watermark = 0 if full else int(store.meta_get(session, WATERMARK_KEY, "0") or 0)
        stmt = (
            select(Job)
            .where(Job.id > watermark)
            .order_by(Job.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        jobs = session.scalars(stmt).all()
        result.high_water = watermark

        # Boards this corpus already knows, so a hit on a board we are already
        # fetching costs no query. Providers times tokens stays small enough to
        # hold in memory even at the tens of thousands the plan anticipates.
        known: set[tuple[str, str]] = {
            (provider, token)
            for provider, token in session.execute(select(Board.provider, Board.token)).all()
        }

        for job in jobs:
            result.scanned += 1
            result.high_water = max(result.high_water, job.id)

            # Structured fields and the body are scanned separately on purpose:
            # only a structured field is trustworthy enough to attribute a board
            # to this job's company. A body link is as likely to be a "see also".
            structured_hits, structured_hints = patterns.scan_many(
                [job.apply_url, job.source_url]
            )
            body_hits, _ = patterns.scan(job.description_html)

            for hit in structured_hits | body_hits:
                result.hits += 1
                attributed = job.company_id if hit in structured_hits else None
                outcome = _record_board(
                    session, known, hit, job, "apply_url", attributed, dry_run
                )
                if outcome == "new":
                    result.new_boards += 1
                    new_by_provider[hit.provider] += 1
                elif outcome == "linked":
                    result.known_boards += 1
                    result.companies_linked += 1
                else:
                    result.known_boards += 1

            for hint in structured_hints:
                result.hints += 1
                if _learn_domain(session, job, hint, dry_run):
                    result.domains_learned += 1
                if not guess_from_hints:
                    continue
                token = patterns.guess_token(hint.domain)
                if not token:
                    continue
                guess = patterns.BoardHit(hint.provider, token)
                outcome = _record_board(
                    session, known, guess, job, "domain_guess", job.company_id, dry_run
                )
                if outcome == "new":
                    result.guessed_boards += 1
                    new_by_provider[hint.provider] += 1

        if not dry_run and jobs:
            store.meta_set(session, WATERMARK_KEY, str(result.high_water))

    result.by_provider = dict(new_by_provider)
    return result


def _record_board(
    session,
    known: set[tuple[str, str]],
    hit: patterns.BoardHit,
    job: Job,
    discovered_via: str,
    company_id: int | None,
    dry_run: bool,
) -> str:
    """Insert or link one board. Returns 'new', 'linked', or 'known'."""
    key = (hit.provider, hit.token)
    if key in known:
        if company_id is None or dry_run:
            return "known"
        board = session.scalars(
            select(Board).where(Board.provider == hit.provider, Board.token == hit.token)
        ).first()
        if board is not None and board.company_id is None:
            board.company_id = company_id
            return "linked"
        return "known"

    known.add(key)
    if dry_run:
        return "new"

    board = Board(
        provider=hit.provider,
        token=hit.token,
        company_id=company_id,
        discovered_via=discovered_via,
        market=job.market,
        status="candidate",
        # next_fetch_at stays NULL, which due_boards treats as due now. A
        # candidate gets exactly one validating fetch on the next sync.
        next_fetch_at=None,
        notes=None if _has_adapter(hit.provider) else UNFETCHABLE_NOTE,
    )
    session.add(board)
    session.flush()
    return "new"


def _has_adapter(provider: str) -> bool:
    from jobhunt import sources as source_registry

    return provider in source_registry.REGISTRY


def _learn_domain(session, job: Job, hint: patterns.DomainHint, dry_run: bool) -> bool:
    """A gh_jid link on the employer's own domain is a free company domain.

    Only from structured fields, and only when the company has none, so this can
    upgrade a name-keyed company row but never overwrite a known domain.
    """
    if dry_run or not job.company_id:
        return False
    company = session.get(Company, job.company_id)
    if company is None or company.domain:
        return False
    company.domain = hint.domain
    return True


def write_report(config: Config, result: HarvestResult) -> pathlib.Path:
    """One JSON file per harvest, so a run can be diffed without the database."""
    out_dir = config.data_dir / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"harvest_{result.high_water}.json"
    path.write_text(json.dumps(dataclasses.asdict(result), indent=2), encoding="utf-8")
    return path
