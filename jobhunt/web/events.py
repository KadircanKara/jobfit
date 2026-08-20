"""Progress events, and the bounded tail a reconnecting browser replays from.

A run outlives any particular browser tab. When one comes back it sends the
last id it saw and gets the rest, so the visible feed matches what actually
happened rather than starting blank mid-run.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import threading
from collections.abc import Iterator
from typing import Any

DEFAULT_CAP = 500


@dataclasses.dataclass(frozen=True)
class Event:
    seq: int
    phase: str
    message: str
    source: str | None = None
    boards_done: int | None = None
    boards_total: int | None = None
    jobs_new: int | None = None
    jobs_total: int | None = None
    level: str = "info"

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in dataclasses.asdict(self).items() if value is not None}


def to_sse(event: Event) -> str:
    """One server-sent-events frame, carrying its id so replay can resume."""
    return f"id: {event.seq}\ndata: {json.dumps(event.as_dict())}\n\n"


class EventLog:
    """An append-only log that remembers its last `cap` events.

    Safe to write from the run thread and read from request handlers.
    """

    def __init__(self, cap: int = DEFAULT_CAP) -> None:
        if cap < 1:
            raise ValueError("an event log needs room for at least one event")
        self._events: collections.deque[Event] = collections.deque(maxlen=cap)
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, *, phase: str, message: str, **counters: Any) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(seq=self._seq, phase=phase, message=message, **counters)
            self._events.append(event)
            return event

    def since(self, last_seq: int) -> list[Event]:
        """Everything after `last_seq` that the log still holds.

        If the browser asks for something already evicted it gets the whole
        tail: a short replay is better than a gap it cannot know about.
        """
        with self._lock:
            return [event for event in self._events if event.seq > last_seq]

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def __iter__(self) -> Iterator[Event]:
        with self._lock:
            return iter(list(self._events))
