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
import os
import pathlib
import traceback
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select

from jobhunt import preferences as prefs_module
from jobhunt import sources as source_registry
from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board, Job, Run, utcnow
from jobhunt.db.session import session_scope
from jobhunt.pipeline.dedupe import apply_clustering
from jobhunt.sources.base import BoardRef


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
    rejected: int = 0
    errors: int = 0
    status: str = "ok"
    error_detail: str | None = None

    def summary(self) -> str:
        return (
            f"{self.source}: {self.status} boards={self.boards} fetched={self.raw_fetched} "
            f"normalized={self.normalized} new={self.new} updated={self.updated} "
            f"unchanged={self.unchanged} deactivated={self.deactivated} "
            f"clustered={self.clustered} errors={self.errors} "
            f"rejected={self.rejected} dead_boards={self.dead_boards} run={self.run_key}"
        )


def new_run_key(now: dt.datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y%m%dT%H%M%S")


def raw_dir(config: Config, source: str, run_key: str) -> pathlib.Path:
    return config.raw_dir / source / run_key


def due_boards(
    config: Config,
    source: str,
    force: bool,
    limit: int,
    only_status: str | None = None,
    market: str | None = None,
    candidate_limit: int | None = None,
) -> list[BoardRef]:
    """Boards this source owes a fetch. Never the whole table.

    The run budget is split deliberately. Boards that already produce jobs are
    served first, in due order, and unvalidated candidates get a bounded slice of
    what is left.

    Ordering candidates first would look right and be wrong: a Common Crawl
    backfill puts thousands of NULL next_fetch_at rows in the table at once, and
    they would monopolise every run for weeks while the boards actually carrying
    the user's jobs went stale. A backfill must drain in the background, not in
    front.
    """
    now = utcnow()
    if candidate_limit is None:
        candidate_limit = int(config.get("sync", "max_candidates_per_run", default=50))

    with session_scope(config.db_path) as session:
        def query(is_candidate: bool, cap: int):
            if cap <= 0:
                return []
            stmt = select(Board).where(Board.provider == source, Board.status != "dead")
            if only_status:
                stmt = stmt.where(Board.status == only_status)
            if market:
                stmt = stmt.where(Board.market == market)
            # Aggregator feeds sort first, ahead of how overdue anything is.
            # Under a small cap they would otherwise be crowded out by thousands
            # of ATS boards, and a feed is where a job appears first, often days
            # before the company's own board is next due.
            order = [Board.discovered_via != "feed"]
            if is_candidate:
                stmt = stmt.where(Board.next_fetch_at.is_(None))
            else:
                stmt = stmt.where(Board.next_fetch_at.is_not(None))
                if not force:
                    stmt = stmt.where(Board.next_fetch_at <= now)
                order.append(Board.next_fetch_at)
            order.append(Board.id)
            return list(session.scalars(stmt.order_by(*order).limit(cap)).all())

        boards = query(False, limit)
        boards += query(True, min(candidate_limit, limit - len(boards)))
        return [
            BoardRef(provider=b.provider, token=b.token, market=b.market, extra={"board_id": b.id})
            for b in boards
        ]


# --- concurrency ---------------------------------------------------------------

LOCK_STALE_SECONDS = 3600


class SyncLock:
    """Advisory lock so a foreground fast pass and a background backfill do not
    fetch the same boards twice.

    Advisory on purpose: a held lock degrades the second run to a no-op with a
    message, it never kills it. Stale locks (a crashed run) expire after an hour
    rather than needing manual cleanup.
    """

    def __init__(self, config: Config) -> None:
        self.path = config.home / "sync.lock"
        self.acquired = False

    def __enter__(self) -> SyncLock:
        if self._held_by_someone_else():
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{os.getpid()} {utcnow().isoformat()}\n", encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.acquired and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    def _held_by_someone_else(self) -> bool:
        if not self.path.exists():
            return False
        try:
            age = utcnow() - dt.datetime.fromisoformat(
                self.path.read_text(encoding="utf-8").split(" ", 1)[1].strip()
            )
        except (OSError, ValueError, IndexError):
            return False
        return age.total_seconds() < LOCK_STALE_SECONDS


def build_adapter(config: Config, source: str):
    """Construct one adapter.

    LinkedIn is the only source whose work units come from saved preferences
    rather than from seeded boards, and the only one whose fetch needs the
    corpus to know which detail pages it can skip. Every other adapter takes
    no arguments at all, so that difference is confined to this one function
    instead of being special-cased at each call site.
    """
    cls = source_registry.get(source)
    if source != "linkedin":
        return cls()
    prefs, _ = prefs_module.load(config)
    known_ids = _known_linkedin_ids(config)
    refs = cls(config=config, known_ids=known_ids).board_refs(prefs)
    return cls(refs, config=config, known_ids=known_ids)


def _known_linkedin_ids(config: Config) -> set[str]:
    """External ids already in the corpus, so `fetch` never re-fetches a detail
    page it has already paid for just to refresh an unchanged description."""
    with session_scope(config.db_path) as session:
        rows = session.scalars(
            select(Job.external_id).where(Job.source == "linkedin")
        ).all()
    return set(rows)


# --- pass 1: fetch ------------------------------------------------------------


def fetch_pass(
    config: Config,
    source: str,
    refs: list[BoardRef],
    run_key: str,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, list[str], list[str]]:
    """Write one raw payload file per board.

    Returns (fetched_count, failed_tokens, messages). Failed tokens come back so
    the caller can age the board toward dead instead of retrying a 404 forever.

    `progress` is called after every board, reached or not, with (done, total,
    token). A dead board still advances the count, because a caller drawing a
    bar is tracking boards attempted rather than boards that answered.

    `should_stop` is checked before each board so a stop request lands within
    one fetch rather than at the end of the source.
    """
    adapter = build_adapter(config, source)
    out_dir = raw_dir(config, source, run_key)
    out_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    failed: list[str] = []
    messages: list[str] = []
    headers = {"User-Agent": config.get("http", "user_agent"), "Accept": "application/json"}
    timeout = float(config.get("http", "timeout_seconds", default=30.0))

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for index, ref in enumerate(refs):
            if should_stop is not None and should_stop():
                break
            if index:
                adapter.rate_limit.sleep()
            try:
                payload = adapter.fetch(ref, client)
            except Exception as exc:  # noqa: BLE001 - one board must never fail the source
                failed.append(ref.token)
                messages.append(f"{ref.provider}/{ref.token}: {type(exc).__name__}: {exc}")
                if progress is not None:
                    progress(index + 1, len(refs), ref.token)
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
            if progress is not None:
                progress(index + 1, len(refs), ref.token)
    return fetched, failed, messages


# The cap is per source, and there are eleven sources, so this is roughly
# 11 x 12 requests plus the feeds, which lands around two to three minutes at
# one request per second. 50 per source would be a quarter of an hour, which is
# not a thing to make someone wait for interactively. Feeds sort ahead of
# everything, so a pass this small still refreshes all of them.
FAST_MAX_BOARDS = 12

DEAD_AFTER_ERRORS = 3


def record_fetch_failures(config: Config, source: str, tokens: list[str]) -> tuple[int, int]:
    """Age a failing board toward dead. PLAN.md section 3.5 validation loop.

    Three consecutive errors and the board is never fetched again. Without this
    the boards table fills with 404s that cost a request every run forever, which
    is the difference between a personal tool and a crawler.

    Returns (newly_dead, rejected_candidates). A candidate that fails is a guess
    that did not pan out, which is the validation loop working, not a fault. It
    is counted separately so a run full of them does not read as degraded and
    train the user to ignore the status.
    """
    if not tokens:
        return 0, 0
    newly_dead = 0
    rejected = 0
    with session_scope(config.db_path) as session:
        boards = session.scalars(
            select(Board).where(Board.provider == source, Board.token.in_(tokens))
        ).all()
        for board in boards:
            board.consecutive_errors += 1
            board.last_fetched_at = utcnow()
            # An unvalidated candidate that fails its first fetch is a bad guess,
            # not a flaky board. Giving it three strikes would mean every wrong
            # domain guess costs three requests instead of one, and Strategies C
            # and D are about to produce tens of thousands of guesses.
            if board.status == "candidate":
                rejected += 1
            if board.status == "candidate" or board.consecutive_errors >= DEAD_AFTER_ERRORS:
                board.status = "dead"
                board.tier = "cold"
                board.next_fetch_at = None
                newly_dead += 1
            else:
                board.next_fetch_at = utcnow() + dt.timedelta(days=1)
    return newly_dead, rejected


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
    adapter = build_adapter(config, source)
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
    # One transaction per board, not one for the whole run. SQLite in WAL mode
    # allows a single writer, and a run that holds the lock for the several
    # minutes a 200-board pass takes makes every other command fail with
    # "database is locked". Committing per board also means a crash halfway
    # through keeps the boards already done.
    for envelope in envelopes:
        ref = BoardRef(envelope["provider"], envelope["token"], envelope["market"])
        with session_scope(config.db_path) as session:
            board = store.get_or_create_board(
                session, ref.provider, ref.token, "sync", ref.market
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

    with session_scope(config.db_path) as session:
        result.clustered = apply_clustering(session, touched_ids)
    return result


# An aggregator feed is the freshness source: it is where a job first appears,
# often days before the company board is next due. Weekly would defeat it.
FEED_REFETCH_DAYS = 1


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
        days = FEED_REFETCH_DAYS if board.discovered_via == "feed" else 7
        board.next_fetch_at = now + dt.timedelta(days=days)
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
    only_status: str | None = None,
    market: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SourceResult:
    """Run one source end to end. Never raises: failures come back on the result."""
    run_key = from_raw or new_run_key()
    result = SourceResult(source=source, run_key=run_key)
    messages: list[str] = []

    try:
        if from_raw is None:
            limit = int(config.get("sync", "max_boards_per_run", default=200))
            refs = due_boards(
                config, source, force, limit, only_status=only_status, market=market
            )
            result.boards = len(refs)
            if not refs:
                result.status = "ok"
                return result
            fetched, failed_tokens, messages = fetch_pass(
                config, source, refs, run_key, progress=progress, should_stop=should_stop
            )
            result.raw_fetched = fetched
            result.dead_boards, result.rejected = record_fetch_failures(
                config, source, failed_tokens
            )
            # A rejected guess is an expected outcome of the validation loop, so
            # it is reported but never counted as an error.
            result.errors = len(failed_tokens) - result.rejected

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
