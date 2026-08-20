"""The progress event log behind the SSE stream.

A closed laptop lid must not lose the feed, so the log keeps a bounded tail and
replays whatever a reconnecting browser missed.
"""
from __future__ import annotations

import pytest

from jobhunt.web import events as events_module


def test_each_event_gets_the_next_sequence_number():
    log = events_module.EventLog()

    first = log.emit(phase="sync", message="greenhouse board 1/50")
    second = log.emit(phase="sync", message="greenhouse board 2/50")

    assert (first.seq, second.seq) == (1, 2)


def test_a_reconnecting_browser_gets_only_what_it_missed():
    log = events_module.EventLog()
    log.emit(phase="sync", message="one")
    log.emit(phase="sync", message="two")
    log.emit(phase="sync", message="three")

    missed = log.since(1)

    assert [event.message for event in missed] == ["two", "three"]


def test_a_browser_that_missed_nothing_gets_nothing():
    log = events_module.EventLog()
    log.emit(phase="sync", message="one")

    assert log.since(1) == []


def test_the_log_evicts_the_oldest_events_past_its_cap():
    log = events_module.EventLog(cap=3)
    for index in range(5):
        log.emit(phase="sync", message=str(index))

    assert [event.message for event in log.since(0)] == ["2", "3", "4"]


def test_a_browser_asking_for_an_evicted_event_gets_the_whole_tail():
    """Better a short replay than a silent gap the user never sees."""
    log = events_module.EventLog(cap=2)
    for index in range(5):
        log.emit(phase="sync", message=str(index))

    assert [event.message for event in log.since(1)] == ["3", "4"]


def test_counters_ride_along_with_the_event():
    log = events_module.EventLog()

    event = log.emit(
        phase="sync", message="greenhouse", source="greenhouse",
        boards_done=34, boards_total=50, jobs_new=3883,
    )

    assert (event.source, event.boards_done, event.jobs_new) == ("greenhouse", 34, 3883)


def test_an_event_serialises_to_one_sse_frame_carrying_its_id():
    log = events_module.EventLog()
    event = log.emit(phase="sync", message="hello")

    frame = events_module.to_sse(event)

    assert frame.startswith("id: 1\n")
    assert frame.endswith("\n\n")
    assert '"message": "hello"' in frame or '"message":"hello"' in frame


@pytest.mark.parametrize("cap", [0, -1])
def test_a_log_with_no_room_is_a_programming_error(cap):
    with pytest.raises(ValueError):
        events_module.EventLog(cap=cap)
