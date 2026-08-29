"""The watchdog only fires on a server that is genuinely doing nothing."""
from __future__ import annotations

import threading

from jobhunt.web import idle


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_quiet_server_expires() -> None:
    fake = FakeClock()
    clock = idle.IdleClock(fake)
    fake.now += 3600.0
    assert idle.expired(clock, 3600.0, lambda: False)


def test_a_request_resets_the_clock() -> None:
    fake = FakeClock()
    clock = idle.IdleClock(fake)
    fake.now += 3599.0
    clock.touch()
    fake.now += 100.0
    assert not idle.expired(clock, 3600.0, lambda: False)


def test_work_in_flight_beats_the_clock() -> None:
    """A tailoring batch makes no requests for as long as an agent takes. The
    process must not be shot out from under it."""
    fake = FakeClock()
    clock = idle.IdleClock(fake)
    fake.now += 7200.0
    assert not idle.expired(clock, 3600.0, lambda: True)
    # And the busy check pushed the deadline out, so the next quiet hour starts now.
    assert not idle.expired(clock, 3600.0, lambda: False)


def test_zero_disables_the_watchdog() -> None:
    fake = FakeClock()
    clock = idle.IdleClock(fake)
    fake.now += 10_000.0
    assert not idle.expired(clock, 0, lambda: False)
    assert idle.watch(clock, 0, lambda: False, lambda: None) is None


def test_the_watchdog_thread_calls_back_once_idle() -> None:
    fake = FakeClock()
    clock = idle.IdleClock(fake)
    fake.now += 3600.0
    fired = threading.Event()
    thread = idle.watch(
        clock, 3600.0, lambda: False, fired.set, interval=0.01
    )
    assert thread is not None
    assert fired.wait(2.0)
    thread.join(2.0)
    assert not thread.is_alive()
