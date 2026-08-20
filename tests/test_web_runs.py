"""The run supervisor.

The rules that matter here: a stop lands quickly and still produces a
shortlist, one bad source does not take the run down with it, and a real
failure is reported rather than swallowed.
"""
from __future__ import annotations

import pytest

from jobhunt.web import events as events_module
from jobhunt.web import runs as runs_module


class FakePipeline:
    """Stands in for sync/rank/gate/shortlist without touching the network."""

    def __init__(self, *, sources=None, boards=2, failing_source=None, rank_raises=False):
        self.sources = sources or ["greenhouse", "ashby"]
        self.boards = boards
        self.failing_source = failing_source
        self.rank_raises = rank_raises
        self.fetched: list[tuple[str, int]] = []
        self.ranked = False
        self.gated: list[int] = []
        self.shortlisted = False
        self.on_fetch = None

    def boards_for(self, source):
        return list(range(self.boards))

    def fetch_board(self, source, board):
        if source == self.failing_source:
            raise ConnectionError("boom")
        self.fetched.append((source, board))
        if self.on_fetch:
            self.on_fetch(len(self.fetched))
        return 7

    def rank(self):
        if self.rank_raises:
            raise RuntimeError("rank exploded")
        self.ranked = True
        return 12

    def gate_batches(self):
        return [[1, 2], [3, 4]]

    def gate(self, batch):
        self.gated.append(len(batch))

    def shortlist(self):
        self.shortlisted = True
        return [{"id": 1, "fit": 0.88}]


def supervisor(pipeline):
    return runs_module.RunSupervisor(pipeline=pipeline, log=events_module.EventLog())


def test_a_clean_run_walks_every_phase_and_reports_completed():
    pipeline = FakePipeline()
    sup = supervisor(pipeline)

    sup.run()

    assert pipeline.ranked and pipeline.shortlisted
    assert pipeline.gated == [2, 2]
    assert sup.state.outcome == "completed"
    assert sup.state.phase == "done"


def test_every_board_fetched_emits_progress():
    pipeline = FakePipeline(sources=["greenhouse"], boards=3)
    sup = supervisor(pipeline)

    sup.run()

    board_events = [e for e in sup.log if e.phase == "sync" and e.source == "greenhouse"]
    assert len(board_events) == 3
    assert board_events[-1].boards_done == 3


def test_a_stop_during_sync_stops_fetching_more_boards():
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 3 else None

    sup.run()

    assert len(pipeline.fetched) == 3


def test_a_stopped_run_still_produces_a_shortlist():
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 1 else None

    sup.run()

    assert pipeline.shortlisted
    assert sup.state.outcome == "stopped_early"


def test_a_stopped_run_still_gates_what_it_collected():
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 1 else None

    sup.run()

    assert pipeline.gated, "a partial corpus is still worth ranking and gating"


def test_a_source_that_fails_is_recorded_and_the_run_carries_on():
    pipeline = FakePipeline(sources=["greenhouse", "ashby"], failing_source="greenhouse")
    sup = supervisor(pipeline)

    sup.run()

    assert sup.state.degraded == ["greenhouse"]
    assert [source for source, _ in pipeline.fetched] == ["ashby", "ashby"]
    assert sup.state.outcome == "completed"


def test_a_failing_source_says_so_in_the_feed():
    pipeline = FakePipeline(sources=["greenhouse"], failing_source="greenhouse")
    sup = supervisor(pipeline)

    sup.run()

    assert any(event.level == "warning" for event in sup.log)


def test_an_unexpected_failure_fails_the_run_and_keeps_the_traceback():
    pipeline = FakePipeline(rank_raises=True)
    sup = supervisor(pipeline)

    sup.run()

    assert sup.state.phase == "failed"
    assert sup.state.outcome == "failed"
    assert "rank exploded" in sup.state.error
    assert not pipeline.shortlisted


def test_two_runs_at_once_are_refused():
    sup = supervisor(FakePipeline())
    sup.state.phase = "sync"

    with pytest.raises(runs_module.RunInProgress):
        sup.run()


def test_counters_track_jobs_seen_across_sources():
    pipeline = FakePipeline(sources=["greenhouse", "ashby"], boards=2)
    sup = supervisor(pipeline)

    sup.run()

    assert sup.state.counters["jobs_total"] == 4 * 7
    assert sup.state.counters["boards_done"] == 4
