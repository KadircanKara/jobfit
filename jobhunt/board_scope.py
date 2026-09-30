"""Which job boards a run fetches, and the on-demand check of all the others.

A normal run fetches only the **relevant** boards: those that have ever posted
a job whose title matches the user's title patterns, plus the aggregator feeds.
It fetches every one of them, every run - no per-run cap, no weekly wait -
because these are the boards the shortlist comes from, and a board fetched
once a week is a week of jobs missed.

Everything else - boards that have posted jobs but never a matching one,
boards that have never posted anything, candidate boards never fetched, and
once, dead boards - is fetched only when the user asks, by "Check other
boards" (`OtherBoardsCheck`). A board that turns out to carry a matching job
becomes relevant then, and normal runs pick it up from there.

Relevance is worked out from the stored jobs against the *current* patterns,
never stored, so changing the titles changes the set at once.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import re
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board, Job, utcnow
from jobhunt.db.session import session_scope
from jobhunt.rank import deterministic
from jobhunt.sources.base import BoardRef

FEED = "feed"
# Set once dead boards have had their one second chance. See `other_boards`.
_DEAD_RECHECKED_KEY = "boards_dead_rechecked_at"
_LAST_CHECK_KEY = "boards_other_checked_at"


def _patterns(config: Config) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """The title patterns stage 1 applies: every market's and the global ones."""
    filters = deterministic.load_filters(config)
    global_rules = filters.get("global") or {}
    required = list(global_rules.get("require_titles_regex") or [])
    for profile in (filters.get("profiles") or {}).values():
        required += list((profile or {}).get("require_titles_regex") or [])
    excluded = list(global_rules.get("exclude_titles_regex") or [])

    def compiled(patterns: list[str]) -> list[re.Pattern[str]]:
        out = []
        for pattern in patterns:
            try:
                out.append(re.compile(pattern))
            except re.error:
                continue
        return out

    return compiled(required), compiled(excluded)


def relevant_board_ids(config: Config) -> set[int]:
    """Boards that have ever posted a job whose title the user is looking for.

    With no title patterns at all every title matches, so every board that
    has posted anything is relevant.
    """
    required, excluded = _patterns(config)
    relevant: set[int] = set()
    with session_scope(config.db_path) as session:
        rows = session.execute(
            select(Job.board_id, Job.title).where(Job.board_id.is_not(None))
        ).all()
    for board_id, title in rows:
        if board_id in relevant:
            continue
        text = title or ""
        if required and not any(p.search(text) for p in required):
            continue
        if any(p.search(text) for p in excluded):
            continue
        relevant.add(board_id)
    return relevant


def _refs(boards: list[Board]) -> list[BoardRef]:
    return [
        BoardRef(provider=b.provider, token=b.token, market=b.market, extra={"board_id": b.id})
        for b in boards
    ]


def relevant_boards(
    config: Config,
    source: str,
    *,
    market: str | None = None,
    limit: int | None = None,
    relevant: set[int] | None = None,
) -> list[BoardRef]:
    """Every relevant board of one source, feeds first. What a normal run fetches."""
    relevant = relevant_board_ids(config) if relevant is None else relevant
    with session_scope(config.db_path) as session:
        stmt = select(Board).where(Board.provider == source, Board.status != "dead")
        if market:
            stmt = stmt.where(Board.market == market)
        boards = [
            b for b in session.scalars(stmt.order_by(Board.discovered_via != FEED, Board.id)).all()
            if b.discovered_via == FEED or b.id in relevant
        ]
        return _refs(boards[:limit] if limit else boards)


def other_boards(
    config: Config,
    source: str,
    *,
    include_dead: bool,
    relevant: set[int] | None = None,
) -> list[BoardRef]:
    """Every board of one source a normal run skips. What the check fetches."""
    relevant = relevant_board_ids(config) if relevant is None else relevant
    with session_scope(config.db_path) as session:
        stmt = select(Board).where(Board.provider == source, Board.discovered_via != FEED)
        if not include_dead:
            stmt = stmt.where(Board.status != "dead")
        boards = [b for b in session.scalars(stmt.order_by(Board.id)).all() if b.id not in relevant]
        return _refs(boards)


def dead_boards_rechecked(config: Config) -> bool:
    with session_scope(config.db_path) as session:
        return store.meta_get(session, _DEAD_RECHECKED_KEY) is not None


def summary(config: Config, sources: list[str]) -> dict[str, Any]:
    """Counts for the button: how many boards a run fetches, how many it skips."""
    relevant = relevant_board_ids(config)
    include_dead = not dead_boards_rechecked(config)
    with session_scope(config.db_path) as session:
        last = store.meta_get(session, _LAST_CHECK_KEY)
    fetched = sum(len(relevant_boards(config, s, relevant=relevant)) for s in sources)
    other = sum(len(other_boards(config, s, include_dead=include_dead, relevant=relevant)) for s in sources)
    return {
        "relevant": fetched,
        "other": other,
        "includes_dead": include_dead,
        "last_checked_at": last,
    }


# --- the on-demand check ---------------------------------------------------------


@dataclasses.dataclass
class SourceProgress:
    done: int = 0
    total: int = 0
    status: str = "queued"  # queued | fetching | storing | done | failed
    new_relevant: int = 0


@dataclasses.dataclass
class CheckState:
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    sources: dict[str, SourceProgress] = dataclasses.field(default_factory=dict)
    became_relevant: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "became_relevant": self.became_relevant,
            "sources": {name: dataclasses.asdict(p) for name, p in self.sources.items()},
        }


class CheckBusy(RuntimeError):
    """A check is already going."""


class OtherBoardsCheck:
    """Fetch every board a normal run skips, once, in the background.

    The fetching runs one thread per platform - they are separate sites, and
    each keeps its own pace and its own 429 handling - but storing runs one
    platform at a time, since SQLite takes one writer.
    """

    def __init__(self, config: Config, sources: Callable[[], list[str]]) -> None:
        self.config = config
        self.sources = sources
        self.state = CheckState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self.state.running:
                raise CheckBusy("the board check is already going")
            self._stop.clear()
            self.state = CheckState(running=True, started_at=utcnow().isoformat(timespec="seconds"))
            self._thread = threading.Thread(target=self._run, name="jobhunt-board-check", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            run(self.config, self.sources(), self.state, should_stop=self._stop.is_set)
        except Exception as exc:  # noqa: BLE001 - a background thread must report, not die
            self.state.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.state.running = False
            self.state.finished_at = utcnow().isoformat(timespec="seconds")


def run(
    config: Config,
    sources: list[str],
    state: CheckState,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    fetch: Callable[..., Any] | None = None,
    finish: Callable[..., Any] | None = None,
) -> CheckState:
    """The check itself. `fetch` and `finish` default to the sync module's."""
    from jobhunt import sync
    from jobhunt.sources import throttle

    fetch = fetch or sync.fetch_pass
    finish = finish or sync.sync_source

    include_dead = not dead_boards_rechecked(config)
    before = relevant_board_ids(config)
    run_key = sync.new_run_key()
    plans: dict[str, list[BoardRef]] = {}
    for source in sources:
        refs = other_boards(config, source, include_dead=include_dead, relevant=before)
        if refs:
            plans[source] = refs
            state.sources[source] = SourceProgress(total=len(refs))

    def fetch_one(source: str) -> tuple[str, Any]:
        progress = state.sources[source]
        progress.status = "fetching"

        def hook(done: int, total: int, _token: str) -> None:
            progress.done = done

        report = throttle.Report()
        outcome = fetch(
            config, source, plans[source], run_key,
            progress=hook, should_stop=should_stop, throttled=report,
        )
        progress.status = "storing"
        return source, (outcome, report)

    fetched: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(plans))) as pool:
        for future in concurrent.futures.as_completed([pool.submit(fetch_one, s) for s in plans]):
            try:
                source, outcome = future.result()
                fetched[source] = outcome
            except Exception as exc:  # noqa: BLE001 - one platform must not sink the others
                state.error = f"{type(exc).__name__}: {exc}"

    for source, ((count, failed, messages), report) in fetched.items():
        finish(
            config, source,
            prefetched=sync.Prefetched(
                run_key=run_key, refs=plans[source], fetched=count, failed=failed,
                messages=messages, throttled=report,
            ),
        )
        state.sources[source].status = "done"

    after = relevant_board_ids(config)
    gained = after - before
    state.became_relevant = len(gained)
    with session_scope(config.db_path) as session:
        by_provider = session.execute(
            select(Board.provider, Board.id).where(Board.id.in_(gained))
        ).all() if gained else []
    for provider, _ in by_provider:
        if provider in state.sources:
            state.sources[provider].new_relevant += 1
    for progress in state.sources.values():
        if progress.status != "done":
            progress.status = "failed"

    with session_scope(config.db_path) as session:
        now = utcnow().isoformat(timespec="seconds")
        store.meta_set(session, _LAST_CHECK_KEY, now)
        if include_dead and not should_stop():
            store.meta_set(session, _DEAD_RECHECKED_KEY, now)
    return state
