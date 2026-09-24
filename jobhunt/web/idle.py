"""Stop a server nobody is using, and only then.

The interface is started by hand and left running, so the process outlives the
terminal that started it. That is the point — marking a job applied an hour
later has to reach something. So the watchdog is off by default, and a server
only retires itself once the browser has gone quiet when --idle-timeout asks it
to.

Two conditions, not one. Silence on the socket is not enough: a tailoring batch
makes no requests for as long as an agent takes to answer, and killing the
process mid-batch would lose the run. So work in flight always wins over the
clock.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Off: an API that stops on its own leaves the page up with nothing behind it.
DEFAULT_IDLE_SECONDS = 0.0

# How often the watchdog looks. Cheap, and it bounds how long past the deadline
# a shutdown can land.
CHECK_INTERVAL = 30.0


class IdleClock:
    """Last time anything touched the server, in monotonic seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._last = clock()

    def touch(self) -> None:
        with self._lock:
            self._last = self._clock()

    def idle_for(self) -> float:
        with self._lock:
            return max(0.0, self._clock() - self._last)


def expired(clock: IdleClock, timeout: float, busy: Callable[[], bool]) -> bool:
    """Whether the server has earned a shutdown right now."""
    if timeout <= 0:  # 0 disables the watchdog entirely.
        return False
    if busy():
        # Work in flight resets the clock: a batch that runs for two hours must
        # not be shot at the end of the first one.
        clock.touch()
        return False
    return clock.idle_for() >= timeout


def watch(
    clock: IdleClock,
    timeout: float,
    busy: Callable[[], bool],
    on_idle: Callable[[], None],
    *,
    interval: float = CHECK_INTERVAL,
    stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Run the watchdog in the background. Returns None when it is disabled."""
    if timeout <= 0:
        return None
    halt = stop or threading.Event()

    def loop() -> None:
        while not halt.wait(interval):
            if expired(clock, timeout, busy):
                on_idle()
                return

    thread = threading.Thread(target=loop, daemon=True, name="jobhunt-idle")
    thread.start()
    return thread
