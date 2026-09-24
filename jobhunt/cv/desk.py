"""A job that runs off the request thread while the page polls it.

The one-time import and a template upload both call an agent that takes
minutes, so both follow the same life: idle, then running, then done or failed,
one at a time. That life lives here once. Each desk supplies only its work, the
fields its snapshot shows, and what accepting the result means.

Every field is read and written under the lock. The page polls from a request
thread while the work thread finishes, and a half-updated desk reads as
"failed" with no reason, which stops the polling for good.
"""
from __future__ import annotations

import threading
from collections.abc import Callable


class Desk:
    """Subclasses set `Failure` and `running_message`, and implement `_clear`."""

    Failure: type[Exception] = RuntimeError
    running_message = "this is still running"

    def __init__(self, *, background: bool = True) -> None:
        self.background = background
        self._lock = threading.Lock()
        self._reset()

    def _clear(self) -> None:
        """Put the subclass's own fields back to empty."""

    def _reset(self) -> None:
        self.state = "idle"  # idle | running | done | failed
        self.error: str | None = None
        self._clear()

    def _launch(self, work: Callable[[], None], name: str) -> None:
        """Run `work` after the caller has marked the desk running, under the lock."""
        if self.background:
            threading.Thread(target=self._guarded, args=(work,), daemon=True, name=name).start()
        else:
            self._guarded(work)

    def _guarded(self, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as exc:  # a background thread has nobody else to tell
            with self._lock:
                self.error, self.state = str(exc), "failed"

    def discard(self) -> None:
        with self._lock:
            if self.state == "running":
                raise self.Failure(self.running_message)
            self._reset()
