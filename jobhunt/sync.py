"""Sync orchestration: fetch pass, then normalize pass, then dedupe.

The two passes are deliberately separate and talk to each other only through
files on disk. `sync --from-raw <run_key>` re-runs normalization against payloads
already fetched, so fixing a parser never costs a re-fetch. PLAN.md non-negotiable 2.

Every source is isolated. A source that raises marks its run degraded and the
command still exits 0 with the other sources' results. PLAN.md non-negotiable 3.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import traceback
from typing import Any

import httpx
from sqlalchemy import select

from jobhunt import sources as source_registry
from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board, Run, utcnow
from jobhunt.db.session import session_scope
from jobhunt.pipeline.dedupe import apply_clustering
from jobhunt.sources.base import BoardRef

# Throwaway phase-2 fixtures. Verified live on 2026-08-20. These get deleted in
# phase 3 the moment Strategy A harvesting produces real tokens.
FIXTURE_BOARDS: list[tuple[str, str, str]] = [
    ("greenhouse", "stripe", "global_remote"),
    ("ashby", "ramp", "global_remote"),
    ("ashby", "openai", "global_remote"),
]


@dataclasses.dataclass
class SourceResult:
    source: str
    run_key: str
    boards: int = 0
    raw_fetched: int = 0
    normalized: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    clustered: int = 0
    dead_boards: int = 0
    errors: int = 0
    status: str = "ok"
    error_detail: str | None = None

    def summary(self) -> str:
        return (
            f"{self.source}: {self.status} boards={self.boards} fetched={self.raw_fetched} "
            f"normalized={self.normalized} new={self.new} updated={self.updated} "
            f"unchanged={self.unchanged} deactivated={self.deactivated} "
            f"clustered={self.clustered} errors={self.errors} "
            f"dead_boards={self.dead_boards} run={self.run_key}"
        )


def new_run_key(now: dt.datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y%m%dT%H%M%S")


def raw_dir(config: Config, source: str, run_key: str) -> pathlib.Path:
    return config.raw_dir / source / run_key


def seed_fixture_boards(config: Config) -> int:
    """Ensure the phase-2 fixture boards exist. Idempotent."""
    added = 0
    with session_scope(config.db_path) as session:
        for provider, token, market in FIXTURE_BOARDS:
            existing = session.scalars(
                select(Board).where(Board.provider == provider, Board.token == token)
            ).first()
            if existing is None:
                store.get_or_create_board(session, provider, token, "fixture", market)
                added += 1
    return added


def due_boards(config: Config, source: str, force: bool, limit: int) -> list[BoardRef]:
    """Boards this source owes a fetch, per next_fetch_at. Never the whole table."""
    now = utcnow()
    with session_scope(config.db_path) as session:
        stmt = select(Board).where(Board.provider == source, Board.status != "dead")
        if not force:
            stmt = stmt.where((Board.next_fetch_at.is_(None)) | (Board.next_fetch_at <= now))
        stmt = stmt.order_by(Board.next_fetch_at.is_(None).desc(), Board.id).limit(limit)
        boards = session.scalars(stmt).all()
        return [
            BoardRef(provider=b.provider, token=b.token, market=b.market, extra={"board_id": b.id})
            for b in boards
        ]


# --- pass 1: fetch ------------------------------------------------------------


def fetch_pass(
    config: Config, source: str, refs: list[BoardRef], run_key: str
) -> tuple[int, list[str], list[str]]:
    """Write one raw payload file per board.

    Returns (fetched_count, failed_tokens, messages). Failed tokens come back so
    the caller can age the board toward dead instead of retrying a 404 forever.
    """
    adapter = source_registry.get(source)()
    out_dir = raw_dir(config, source, run_key)
    out_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    failed: list[str] = []
    messages: list[str] = []
    headers = {"User-Agent": config.get("http", "user_agent"), "Accept": "application/json"}
    timeout = float(config.get("http", "timeout_seconds", default=30.0))

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for index, ref in enumerate(refs):
            if index:
                adapter.rate_limit.sleep()
            try:
                payload = adapter.fetch(ref, client)
            except Exception as exc:  # noqa: BLE001 - one board must never fail the source
                failed.append(ref.token)
                messages.append(f"{ref.provider}/{ref.token}: {type(exc).__name__}: {exc}")
                continue
            envelope = {
                "run_key": run_key,
                "source": source,
                "provider": ref.provider,
                "token": ref.token,
                "market": ref.market,
                "board_id": ref.extra.get("board_id"),
                "fetched_at": utcnow().isoformat(),
                "payload": payload,
            }
            (out_dir / f"{ref.key}.json").write_text(
                json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
            )
            fetched += 1
    return fetched, failed, messages


DEAD_AFTER_ERRORS = 3


def record_fetch_failures(config: Config, source: str, tokens: list[str]) -> int:
    """Age a failing board toward dead. PLAN.md section 3.5 validation loop.

    Three consecutive errors and the board is never fetched again. Without this
    the boards table fills with 404s that cost a request every run forever, which
    is the difference between a personal tool and a crawler.
    """
    if not tokens:
        return 0
    newly_dead = 0
    with session_scope(config.db_path) as session:
        boards = session.scalars(
            select(Board).where(Board.provider == source, Board.token.in_(tokens))
        ).all()
        for board in boards:
            board.consecutive_errors += 1
            board.last_fetched_at = utcnow()
            if board.consecutive_errors >= DEAD_AFTER_ERRORS:
                board.status = "dead"
                board.next_fetch_at = None
                newly_dead += 1
            else:
                board.next_fetch_at = utcnow() + dt.timedelta(days=1)
    return newly_dead


# --- pass 2: normalize and store ---------------------------------------------


def load_raw(config: Config, source: str, run_key: str) -> list[dict[str, Any]]:
    directory = raw_dir(config, source, run_key)
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def normalize_pass(
    config: Config, source: str, run_key: str, dry_run: bool = False
) -> SourceResult:
    adapter = source_registry.get(source)()
    result = SourceResult(source=source, run_key=run_key)
    envelopes = load_raw(config, source, run_key)
    result.boards = len(envelopes)
    result.raw_fetched = len(envelopes)

    if dry_run:
        for envelope in envelopes:
            ref = BoardRef(envelope["provider"], envelope["token"], envelope["market"])
            postings = list(adapter.normalize(envelope["payload"], ref))
            result.normalized += len(postings)
        return result

    touched_ids: list[int] = []
    with session_scope(config.db_path) as session:
        for envelope in envelopes:
            ref = BoardRef(envelope["provider"], envelope["token"], envelope["market"])
            board = store.get_or_create_board(
                session, ref.provider, ref.token, "fixture", ref.market
            )
            seen: set[str] = set()
            count = 0
            for posting in adapter.normalize(envelope["payload"], ref):
                job, outcome = store.upsert_posting(session, posting, board)
                touched_ids.append(job.id)
                seen.add(posting.external_id)
                count += 1
                setattr(result, outcome, getattr(result, outcome) + 1)
            result.normalized += count
            result.deactivated += store.deactivate_missing(session, board, seen)
            _record_board_outcome(board, count)
        result.clustered = apply_clustering(session, touched_ids)
    return result


def _record_board_outcome(board: Board, job_count: int) -> None:
    """Tier promotion and next_fetch_at.

    Phase 2 has no ranker, so 'hot' cannot yet mean 'produced a job that passed the
    filter'. Until phase 5 wires that in, a board with jobs is warm and a board
    without is cold, which keeps the cadence honest without faking a signal.
    """
    now = utcnow()
    board.last_fetched_at = now
    board.last_job_count = job_count
    board.consecutive_errors = 0
    if job_count > 0:
        board.status = "validated"
        board.tier = "warm"
        board.next_fetch_at = now + dt.timedelta(days=7)
    else:
        board.status = "empty"
        board.tier = "cold"
        board.next_fetch_at = now + dt.timedelta(days=30)


# --- the command --------------------------------------------------------------


def sync_source(
    config: Config,
    source: str,
    force: bool = False,
    dry_run: bool = False,
    from_raw: str | None = None,
) -> SourceResult:
    """Run one source end to end. Never raises: failures come back on the result."""
    run_key = from_raw or new_run_key()
    result = SourceResult(source=source, run_key=run_key)
    messages: list[str] = []

    try:
        if from_raw is None:
            limit = int(config.get("sync", "max_boards_per_run", default=200))
            refs = due_boards(config, source, force, limit)
            result.boards = len(refs)
            if not refs:
                result.status = "ok"
                return result
            fetched, failed_tokens, messages = fetch_pass(config, source, refs, run_key)
            result.raw_fetched = fetched
            result.errors = len(failed_tokens)
            result.dead_boards = record_fetch_failures(config, source, failed_tokens)

        normalized = normalize_pass(config, source, run_key, dry_run=dry_run)
        for field in ("normalized", "new", "updated", "unchanged", "deactivated", "clustered"):
            setattr(result, field, getattr(normalized, field))
        if from_raw is not None:
            result.boards = normalized.boards
            result.raw_fetched = normalized.raw_fetched

        if result.errors and result.raw_fetched:
            result.status = "degraded"
        elif result.errors:
            result.status = "failed"
        result.error_detail = "\n".join(messages) or None
    except Exception as exc:  # noqa: BLE001 - source isolation
        result.status = "failed"
        result.errors += 1
        result.error_detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    if not dry_run:
        try:
            _write_run_row(config, result)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never sink a run
            result.error_detail = f"{result.error_detail or ''}\nrun row not written: {exc}".strip()
    return result


def _write_run_row(config: Config, result: SourceResult) -> None:
    with session_scope(config.db_path) as session:
        session.add(
            Run(
                run_key=f"{result.source}-{result.run_key}",
                market=None,
                started_at=utcnow(),
                finished_at=utcnow(),
                source=result.source,
                raw_fetched=result.raw_fetched,
                new_jobs=result.new,
                updated_jobs=result.updated,
                errors=result.errors,
                status=result.status,
                error_detail=result.error_detail,
            )
        )
