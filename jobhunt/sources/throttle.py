"""Waiting out a job board that says it is being asked too often.

A 429 is not a board failing. It used to be counted as one, and a board that
fails three times in a row - or once, before it has ever worked - is marked
dead and never fetched again: Personio's 429s killed 28 live boards that way.

So `sync.fetch_pass` treats a 429 as a request to wait. It waits what the
board asks (`Retry-After`), or 30 seconds doubling per repeat, and asks for the
same board again. A source that is still refusing after `MAX_WAIT_PER_SOURCE`
seconds of waiting in one run stops there: the boards it did not reach keep
their due date and are fetched next run, and none of them is marked failed.

Every 429 is logged (`history`) with how many requests the source had made in
the run before it and the pause between requests at the time, so each
source's real limit can be read off rather than guessed.
"""
from __future__ import annotations

import dataclasses
import json
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import utcnow
from jobhunt.db.session import session_scope

FIRST_WAIT = 30.0
# One wait longer than this is not worth sitting through; a Retry-After asking
# for more ends the source's run instead.
MAX_SINGLE_WAIT = 120.0
# All the waiting one source may do in one run.
MAX_WAIT_PER_SOURCE = 300.0

_KEY = "board_refusals"
KEPT = 200


@dataclasses.dataclass
class Report:
    """What throttling did to one source's fetch pass."""

    refusals: int = 0
    waited: float = 0.0
    # Boards not reached because the source kept refusing. Left due, not failed.
    left: int = 0


def is_throttle(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def retry_after(exc: httpx.HTTPStatusError) -> float | None:
    raw = exc.response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def wait_for(exc: httpx.HTTPStatusError, attempt: int) -> float:
    """Seconds to wait before asking again. `attempt` counts earlier 429s for this board."""
    asked = retry_after(exc)
    return asked if asked is not None else FIRST_WAIT * (2**attempt)


def pause(seconds: float, should_stop: Callable[[], bool] | None = None) -> bool:
    """Sleep, a second at a time so a stop request still lands. False if stopped."""
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return True
        if should_stop is not None and should_stop():
            return False
        time.sleep(min(1.0, left))


def note(
    config: Config,
    source: str,
    token: str,
    exc: httpx.HTTPStatusError,
    *,
    requests_this_run: int,
    wait: float,
    pause_seconds: float,
) -> None:
    event = {
        "at": utcnow().isoformat(timespec="seconds"),
        "source": source,
        "board": token,
        "status": exc.response.status_code,
        "retry_after": retry_after(exc),
        "requests_this_run": requests_this_run,
        "wait": wait,
        "pause_seconds": pause_seconds,
    }
    with session_scope(config.db_path) as session:
        events = _load(store.meta_get(session, _KEY))
        store.meta_set(session, _KEY, json.dumps([*events, event][-KEPT:]))


def history(config: Config) -> list[dict[str, Any]]:
    """Every board 429 logged so far, oldest first."""
    with session_scope(config.db_path) as session:
        return _load(store.meta_get(session, _KEY))


def _load(raw: str | None) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []
