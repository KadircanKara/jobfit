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
from jobhunt.sources import throttle
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
    # Refs whose fetch ended early - a crawl guard refused, or the source did.
    # Not an error: nothing failed, we simply did not see the whole listing.
    truncated: int = 0
    # Refs a fetch budget kept off the wire entirely. Also not an error, but a
    # run made of these does no work at all and must never report a bare `ok`.
    refused: int = 0
    # Boards not reached because the source kept answering 429. Left due for
    # the next run, never marked failed.
    throttled: int = 0
    status: str = "ok"
    error_detail: str | None = None

    def summary(self) -> str:
        return (
            f"{self.source}: {self.status} boards={self.boards} fetched={self.raw_fetched} "
            f"normalized={self.normalized} new={self.new} updated={self.updated} "
            f"unchanged={self.unchanged} deactivated={self.deactivated} "
            f"clustered={self.clustered} errors={self.errors} truncated={self.truncated} "
            f"refused={self.refused} throttled={self.throttled} "
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
    corpus to know which detail pages it can skip. Every other adapter takes no
    arguments at all, so that difference is confined to this one function
    instead of being special-cased at each call site.

    Without this branch, `jobhunt sync --source linkedin` would build the
    adapter, get zero refs from the inherited `discover()` (which yields
    nothing without `set_refs`), and silently do nothing while reporting no
    error - a working-looking source that never fetches a job.
    """
    cls = source_registry.get(source)
    if source != "linkedin":
        return cls()
    prefs, _ = prefs_module.load(config)
    adapter = cls(config=config, known_ids=_known_ids_for(config, source))
    adapter.set_refs(adapter.board_refs(prefs))
    return adapter


def _known_ids_for(config: Config, source: str) -> set[str]:
    """External ids already in the corpus for one source, so a generated-ref
    adapter can skip re-fetching a detail page it already has."""
    with session_scope(config.db_path) as session:
        rows = session.scalars(select(Job.external_id).where(Job.source == source)).all()
    return set(rows)


# --- pass 1: fetch ------------------------------------------------------------


def fetch_pass(
    config: Config,
    source: str,
    refs: list[BoardRef],
    run_key: str,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    throttled: throttle.Report | None = None,
) -> tuple[int, list[str], list[str]]:
    """Write one raw payload file per board.

    Returns (fetched_count, failed_tokens, messages). Failed tokens come back so
    the caller can age the board toward dead instead of retrying a 404 forever.

    `progress` is called after every board, reached or not, with (done, total,
    token). A dead board still advances the count, because a caller drawing a
    bar is tracking boards attempted rather than boards that answered.

    `should_stop` is checked before each board so a stop request lands within
    one fetch rather than at the end of the source.

    A 429 is waited out and the same board asked again, never counted as the
    board failing; a source still refusing after its waiting allowance stops,
    and the boards it did not reach are left for the next run. `throttled`,
    if given, is filled in with what that cost. See sources/throttle.py.
    """
    throttled = throttled if throttled is not None else throttle.Report()
    requests = 0
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
            if index and adapter.still_fetching():
                adapter.rate_limit.sleep()
            attempt = 0
            outcome = "ok"
            while True:
                requests += 1
                try:
                    payload = adapter.fetch(ref, client)
                except Exception as exc:  # noqa: BLE001 - one board must never fail the source
                    if not throttle.is_throttle(exc):
                        failed.append(ref.token)
                        response = getattr(exc, "response", None)
                        why = (
                            f"redirected to {response.request.url}, board gone"
                            if isinstance(response, httpx.Response) and throttle.left_host(response)
                            else f"{type(exc).__name__}: {exc}"
                        )
                        messages.append(f"{ref.provider}/{ref.token}: {why}")
                        outcome = "failed"
                        break
                    wait = throttle.wait_for(exc, attempt)
                    throttled.refusals += 1
                    throttle.note(
                        config, source, ref.token, exc,
                        requests_this_run=requests, wait=wait,
                        pause_seconds=getattr(adapter.rate_limit, "delay_seconds", 0.0),
                    )
                    if (
                        wait > throttle.MAX_SINGLE_WAIT
                        or throttled.waited + wait > throttle.MAX_WAIT_PER_SOURCE
                        or not throttle.pause(wait, should_stop)
                    ):
                        outcome = "throttled"
                        break
                    throttled.waited += wait
                    attempt += 1
                    continue
                break
            if outcome == "throttled":
                throttled.left = len(refs) - index
                messages.append(
                    f"{source}: still rate limited after waiting {throttled.waited:.0f}s; "
                    f"{throttled.left} boards left for the next run"
                )
                break
            if outcome == "failed":
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
                # From the adapter, not from `payload`: `payload` is a verbatim
                # third-party response body on several adapters, and a bare
                # `truncated` key there would collide with anything upstream
                # ever happens to name the same way. See `_is_truncated`.
                "truncated": adapter.was_truncated(),
                # Same reasoning, different question: this ref never went out at
                # all. See SourceAdapter.was_refused.
                "refused": adapter.was_refused(),
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


def _is_truncated(envelope: dict[str, Any]) -> bool:
    """Whether this payload's fetch ended before it saw the whole listing.

    The flag lives on the envelope, written by `fetch_pass` from the adapter's
    `was_truncated()` - never inside `payload`, which is the verbatim
    third-party response body on several adapters (see remotive, jobicy,
    greenhouse, arbeitnow) and could ship its own top-level `truncated` field
    by coincidence. Only the LinkedIn adapter has ever reported this; every
    other fetch always sees a complete listing or an error.

    A stored envelope written before the flag moved to the top level (only
    LinkedIn ever set it, and only inside `payload`) has no top-level key at
    all, so that shape is read as a fallback rather than misread as complete.
    """
    if "truncated" in envelope:
        return bool(envelope.get("truncated"))
    if envelope.get("source") == "linkedin":
        payload = envelope.get("payload")
        return bool(isinstance(payload, dict) and payload.get("truncated"))
    return False


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
        truncated = _is_truncated(envelope)
        if envelope.get("refused"):
            result.refused += 1
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
            # A partial listing is not evidence that anything disappeared from it.
            # Retiring jobs on the strength of a fetch that was cut short is how a
            # source that keeps getting refused loses its whole corpus.
            if truncated:
                result.truncated += 1
            else:
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


@dataclasses.dataclass
class Prefetched:
    """A fetch pass already done elsewhere, handed to `sync_source` to finish.

    The board check fetches several platforms at once on threads, then stores
    them one at a time; this carries each fetch's outcome across.
    """

    run_key: str
    refs: list[BoardRef]
    fetched: int
    failed: list[str]
    messages: list[str]
    throttled: throttle.Report


def plan_refs(
    config: Config,
    source: str,
    force: bool = False,
    only_status: str | None = None,
    market: str | None = None,
) -> list[BoardRef]:
    """What one source fetches this run: its searches, or its boards.

    A board source fetches every relevant board (see board_scope), or with
    `only_status` the boards `due_boards` owes that status.
    """
    limit = int(config.get("sync", "max_boards_per_run", default=200))
    # A generated-ref source (LinkedIn) has no Board rows to query: its
    # refs come from preferences, fresh every run, via its own
    # `discover()`. Everything else is still owed its fetch by
    # `due_boards()`. The cap applies either way, so a preference set
    # with forty titles cannot turn into a four-hundred-search run.
    adapter_cls = source_registry.get(source)
    if adapter_cls.generates_refs:
        # `only_status="candidate"` means "prove out unvalidated board
        # guesses" (jobhunt boards --validate, jobhunt discover); a
        # generated ref is never a candidate board, so there is nothing
        # to validate here. A `market` filter (e.g. `sync --market yc`)
        # is meant to narrow which boards run; a generated source has
        # no per-ref market to narrow, only its own fixed one, so it
        # runs only when the filter already matches it. Either way this
        # must stay a no-op rather than kick off a full preference-
        # driven crawl from a command whose contract is "just boards".
        if only_status is not None or (market is not None and market != adapter_cls.market):
            return []
        return list(build_adapter(config, source).discover())[:limit]
    if only_status is not None:
        return due_boards(config, source, force, limit, only_status=only_status, market=market)
    from jobhunt import board_scope

    cap = config.get("sync", "max_relevant_boards_per_run", default=None)
    return board_scope.relevant_boards(config, source, market=market, limit=int(cap) if cap else None)


def prefetch(
    config: Config,
    source: str,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Prefetched:
    """Plan and fetch one source, leaving the storing to `sync_source(prefetched=...)`."""
    run_key = new_run_key()
    refs = plan_refs(config, source)
    report = throttle.Report()
    fetched, failed, messages = (
        fetch_pass(config, source, refs, run_key, progress=progress, should_stop=should_stop,
                   throttled=report)
        if refs else (0, [], [])
    )
    return Prefetched(
        run_key=run_key, refs=refs, fetched=fetched, failed=failed, messages=messages,
        throttled=report,
    )


def prefetch_all(
    config: Config,
    sources: list[str],
    progress: Callable[[str, int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[dict[str, Prefetched], dict[str, str]]:
    """Fetch every source at once, one thread each.

    They are separate sites, each with its own pace and its own 429 handling,
    so nothing is gained by one waiting on another: a run over two thousand
    boards fetched one platform after the next took as long as all of them
    added up. Only the fetching overlaps. Storing still happens one source at a
    time afterwards, since SQLite takes one writer.

    Returns (prefetched, errors). A source whose thread raised is left out of
    the first and named in the second, so the caller can fetch it the old way.
    """
    import concurrent.futures

    def one(source: str) -> Prefetched:
        hook = (lambda done, total, token: progress(source, done, total, token)) if progress else None
        return prefetch(config, source, progress=hook, should_stop=should_stop)

    done: dict[str, Prefetched] = {}
    errors: dict[str, str] = {}
    if not sources:
        return done, errors
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(sources), thread_name_prefix="jobhunt-fetch"
    ) as pool:
        futures = {pool.submit(one, source): source for source in sources}
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                done[source] = future.result()
            except Exception as exc:  # noqa: BLE001 - one source must not sink the others
                errors[source] = f"{type(exc).__name__}: {exc}"
    return done, errors


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
    prefetched: Prefetched | None = None,
) -> SourceResult:
    """Run one source end to end. Never raises: failures come back on the result.

    A board source fetches every relevant board (see board_scope), or with
    `only_status` the boards `due_boards` owes that status. `prefetched` skips
    the fetch and finishes one done elsewhere.
    """
    run_key = from_raw or (prefetched.run_key if prefetched else new_run_key())
    result = SourceResult(source=source, run_key=run_key)
    messages: list[str] = []

    try:
        if prefetched is not None:
            result.boards = len(prefetched.refs)
            if not prefetched.refs:
                result.status = "ok"
                return result
            result.throttled = prefetched.throttled.left
            result.raw_fetched = prefetched.fetched
            messages = list(prefetched.messages)
            result.dead_boards, result.rejected = record_fetch_failures(
                config, source, prefetched.failed
            )
            result.errors = len(prefetched.failed) - result.rejected
        elif from_raw is None:
            refs = plan_refs(config, source, force, only_status, market)
            result.boards = len(refs)
            if not refs:
                result.status = "ok"
                return result
            throttled = throttle.Report()
            fetched, failed_tokens, messages = fetch_pass(
                config, source, refs, run_key, progress=progress, should_stop=should_stop,
                throttled=throttled,
            )
            result.throttled = throttled.left
            result.raw_fetched = fetched
            result.dead_boards, result.rejected = record_fetch_failures(
                config, source, failed_tokens
            )
            # A rejected guess is an expected outcome of the validation loop, so
            # it is reported but never counted as an error.
            result.errors = len(failed_tokens) - result.rejected

        normalized = normalize_pass(config, source, run_key, dry_run=dry_run)
        for field in (
            "normalized", "new", "updated", "unchanged", "deactivated", "clustered", "truncated",
            "refused",
        ):
            setattr(result, field, getattr(normalized, field))
        if from_raw is not None:
            result.boards = normalized.boards
            result.raw_fetched = normalized.raw_fetched

        if result.errors and result.raw_fetched:
            result.status = "degraded"
        elif result.errors:
            result.status = "failed"
        # A run that stopped fetching partway through did not fail, but it did not
        # do the job either, and "ok" is the one thing it must not claim.
        if result.truncated:
            result.status = "failed" if result.status == "failed" else "degraded"
            messages.append(
                f"{result.truncated} of {result.boards} searches ended early "
                f"(crawl guard or the source refused); their jobs were left active"
            )
        if result.throttled:
            result.status = "failed" if result.status == "failed" else "degraded"
        # A budget refusal writes the same empty envelope a search that found
        # nothing writes, so without this a run that spent its whole allowance
        # before starting reports `ok` with zero jobs and no reason.
        if result.refused:
            result.status = "failed" if result.status == "failed" else "degraded"
            messages.append(
                f"{result.refused} of {result.boards} searches never ran "
                f"(the fetch budget refused them); nothing was fetched for those"
            )
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
