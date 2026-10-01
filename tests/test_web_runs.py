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

    def __init__(
        self,
        *,
        sources=None,
        boards=2,
        failing_source=None,
        rank_raises=False,
        unreadable_batch=None,
        gate_rounds=1,
    ):
        self._sources = sources or ["greenhouse", "ashby"]
        self.boards = boards
        self.failing_source = failing_source
        self.rank_raises = rank_raises
        # The label of a batch the gate cannot read, as a live one behaves.
        self.unreadable_batch = unreadable_batch
        # How many rounds of work the backlog holds. The live `emit` returns
        # the next unscored slice each call and eventually returns nothing;
        # this drains the same way so a looping gate can terminate.
        self.gate_rounds = gate_rounds
        self.rounds_served = 0
        self.fetched: list[tuple[str, int]] = []
        self.ranked = False
        self.gated: list[int] = []
        self.shortlisted = False
        self.on_fetch = None

    def sources(self):
        return self._sources

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
        return runs_module.RankReport(
            corpus=40,
            scored=30,
            passed=12,
            failed=18,
            skipped=10,
            by_market={"eu": 8, "uk": 4},
            reasons=[
                runs_module.DropReason("salary_below", "salary below the floor", 11, True),
                runs_module.DropReason("tz_overlap", "timezone overlap below the minimum", 7),
            ],
        )

    def gate_batches(self):
        if self.rounds_served >= self.gate_rounds:
            return runs_module.GatePlan()
        self.rounds_served += 1
        return runs_module.GatePlan(
            batches=[
                runs_module.GateBatch(label="eu", size=2, payload=[1, 2]),
                runs_module.GateBatch(label="uk", size=2, payload=[3, 4]),
            ],
            jobs=4,
            held_by_company_cap=3,
            bar=0.7,
        )

    def gate(self, batch):
        self.gated.append(batch.size)
        if batch.label == self.unreadable_batch:
            return []
        return [
            {"job_id": job_id, "title": f"job {job_id}", "company": "Acme",
             "score": 0.8, "reasoning": "fits", "red_flags": []}
            for job_id in batch.payload
        ]

    def shortlist(self):
        self.shortlisted = True
        return [
            {"job_id": 1, "fit": 0.88, "below_bar": False},
            {"job_id": 2, "fit": 0.41, "below_bar": True},
        ]


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


def test_stop_kills_the_run_where_it_stands():
    """Stop is a kill: no ranking, no gating, no shortlist built from half a
    corpus. Pause is the one that keeps the work."""
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 1 else None

    sup.run()

    assert not pipeline.ranked and not pipeline.gated and not pipeline.shortlisted
    assert sup.state.phase == "stopped"
    assert sup.state.outcome == "killed"


def test_a_kill_lands_within_one_board():
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 3 else None

    sup.run()

    assert len(pipeline.fetched) == 3


def test_a_killed_run_is_not_resumable():
    pipeline = FakePipeline(sources=["greenhouse"], boards=10)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_stop() if count == 1 else None
    sup.run()

    assert not sup.state.resumable
    with pytest.raises(runs_module.RunInProgress, match="no paused run"):
        sup.resume()


# --- pause and resume -----------------------------------------------------------


def test_a_pause_stops_where_it_is_and_says_it_can_be_resumed():
    pipeline = FakePipeline(sources=["greenhouse", "ashby"], boards=2)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_pause() if count == 2 else None

    sup.run()

    assert sup.state.phase == "paused"
    assert sup.state.outcome is None
    assert sup.state.resumable
    assert not pipeline.shortlisted, "a paused run has not finished"


def test_resuming_does_not_refetch_a_source_it_already_finished():
    """The whole point of resume: the boards already paid for stay paid for."""
    pipeline = FakePipeline(sources=["greenhouse", "ashby"], boards=2)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_pause() if count == 2 else None
    sup.run()
    assert sup.state.done_sources == ["greenhouse"]

    pipeline.on_fetch = None
    sup.run_resumed()

    assert [source for source, _ in pipeline.fetched] == [
        "greenhouse", "greenhouse", "ashby", "ashby",
    ], "greenhouse was fetched once, before the pause"
    assert sup.state.outcome == "completed"
    assert pipeline.shortlisted


def test_a_source_abandoned_partway_is_fetched_again_on_resume():
    """Half a source is not a finished source, and recording it as one would
    silently drop the rest of its boards."""
    pipeline = FakePipeline(sources=["greenhouse"], boards=4)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_pause() if count == 2 else None

    sup.run()

    assert sup.state.done_sources == [], "it never got to the end of greenhouse"


def test_resuming_a_run_that_was_never_paused_is_refused():
    sup = supervisor(FakePipeline())

    with pytest.raises(runs_module.RunInProgress, match="no paused run"):
        sup.resume()


def test_a_pause_between_phases_is_honoured():
    pipeline = FakePipeline()
    sup = supervisor(pipeline)
    original = pipeline.rank

    def rank_then_pause():
        sup.request_pause()
        return original()

    pipeline.rank = rank_then_pause
    sup.run()

    assert sup.state.phase == "paused"
    assert pipeline.ranked and not pipeline.gated


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


# --- what the run says about rank, the gate and the bar ------------------------


def test_the_rank_report_survives_on_the_state():
    sup = supervisor(FakePipeline())

    sup.run()

    assert sup.state.rank.scored == 30
    assert sup.state.rank.passed == 12
    assert sup.state.counters["passed"] == 12, "the phase strip still reads this"
    assert sup.state.rank.reasons[0].code == "salary_below"


def test_the_heaviest_drop_reasons_reach_the_feed():
    sup = supervisor(FakePipeline())

    sup.run()

    messages = [event.message for event in sup.log if event.phase == "rank"]
    assert any("12 of 30" in message for message in messages)
    assert any("salary below the floor (11)" in message for message in messages)


def test_the_gate_plan_is_known_before_the_first_batch_runs():
    sup = supervisor(FakePipeline())

    sup.run()

    plan = sup.state.gate.plan
    assert [batch.label for batch in plan.batches] == ["eu", "uk"]
    assert plan.jobs == 4
    assert plan.held_by_company_cap == 3
    assert plan.bar == 0.7


def test_every_gated_batch_ends_marked_done_with_its_verdicts_kept():
    sup = supervisor(FakePipeline())

    sup.run()

    assert [batch.status for batch in sup.state.gate.plan.batches] == ["done", "done"]
    assert sup.state.gate.scored == 4
    assert [verdict["job_id"] for verdict in sup.state.gate.verdicts] == [1, 2, 3, 4]


def test_an_unreadable_batch_is_marked_rather_than_passed_over():
    """The gate already gives up on a batch after one retry. Until now the run
    said nothing, and those jobs looked like they had simply scored badly."""
    pipeline = FakePipeline(unreadable_batch="uk")
    sup = supervisor(pipeline)

    sup.run()

    statuses = [batch.status for batch in sup.state.gate.plan.batches]
    assert statuses == ["done", "ungated"]
    assert sup.state.gate.ungated == 2
    assert sup.state.gate.scored == 2
    assert any(
        event.level == "warning" and "ungated" in event.message
        for event in sup.log
    )


def test_an_empty_gate_plan_says_so_and_gates_nothing():
    pipeline = FakePipeline()
    pipeline.gate_batches = lambda: runs_module.GatePlan()
    sup = supervisor(pipeline)

    sup.run()

    assert pipeline.gated == []
    assert sup.state.outcome == "completed"
    assert any("nothing new to gate" in event.message for event in sup.log)


def test_only_jobs_above_the_bar_are_counted_as_shortlisted():
    """Near misses ride along in the same list so the table can draw the cut,
    but they were never exported and must not be counted as kept."""
    sup = supervisor(FakePipeline())

    sup.run()

    assert len(sup.state.results) == 2
    messages = [event.message for event in sup.log if event.phase == "shortlist"]
    assert messages[0] == "checking the postings are still open"
    message = messages[-1]
    assert message.startswith("1 jobs above the bar")
    assert "1 near misses" in message


def test_verdicts_do_not_grow_without_bound():
    pipeline = FakePipeline()
    big = list(range(runs_module.VERDICT_CAP * 2))
    slices = [
        runs_module.GatePlan(
            batches=[runs_module.GateBatch(label="eu", size=len(big), payload=big)],
            jobs=len(big),
        )
    ]
    # One slice, then dry - the way `emit` behaves once every job has a verdict.
    pipeline.gate_batches = lambda: slices.pop() if slices else runs_module.GatePlan()
    sup = supervisor(pipeline)

    sup.run()

    assert len(sup.state.gate.verdicts) == runs_module.VERDICT_CAP
    assert sup.state.gate.scored == len(big), "the count is not capped, only the list"


# --- what gets written down -----------------------------------------------------


def test_every_resting_point_is_saved():
    """A run that is only in memory is a run that a restart throws away."""
    saved = []
    sup = runs_module.RunSupervisor(
        pipeline=FakePipeline(), log=events_module.EventLog(),
        store=lambda state: saved.append(state.phase), clock=lambda: "2026-08-22T10:00:00",
    )

    sup.run(run_id="20260822-100000")

    assert saved[-1] == "done"
    assert "sync" in saved and "rank" in saved
    assert sup.state.run_id == "20260822-100000"
    assert sup.state.started_at and sup.state.finished_at


def test_a_paused_run_is_saved_so_it_survives_a_restart():
    saved = []
    pipeline = FakePipeline(sources=["greenhouse"], boards=4)
    sup = runs_module.RunSupervisor(
        pipeline=pipeline, log=events_module.EventLog(), store=saved.append,
    )
    pipeline.on_fetch = lambda count: sup.request_pause() if count == 2 else None

    sup.run()

    assert saved[-1].phase == "paused"
    assert saved[-1].finished_at is None, "a paused run has not finished"


# --- the gate keeps going until the backlog is drained -------------------------
#
# A run used to gate one slice and stop, leaving every other job that passed the
# deterministic filter sitting unscored until someone pressed Start again. With
# 382 passed jobs and a batch of 20 that is nineteen runs to see them all.


def test_the_gate_keeps_pulling_slices_until_there_is_nothing_left():
    pipeline = FakePipeline(gate_rounds=3)
    sup = supervisor(pipeline)

    sup.run()

    assert pipeline.gated == [2, 2, 2, 2, 2, 2], "three rounds of two batches"
    assert sup.state.gate.rounds == 3


def test_the_round_cap_stops_a_run_before_it_drains_everything():
    pipeline = FakePipeline(gate_rounds=10)
    sup = runs_module.RunSupervisor(
        pipeline=pipeline, log=events_module.EventLog(), max_gate_rounds=2
    )

    sup.run()

    assert sup.state.gate.rounds == 2
    assert pipeline.gated == [2, 2, 2, 2], "stopped at the cap with backlog left"


def test_a_round_that_scores_nothing_stops_the_loop_rather_than_spinning():
    """The guard against an unreadable slice being re-emitted forever.

    An unreadable batch leaves its jobs unscored, so the next `emit` hands back
    the very same jobs. Without this stop the run would burn every remaining
    round on the same slice it already cannot read.
    """
    pipeline = FakePipeline(gate_rounds=10, unreadable_batch="eu")
    pipeline.gate = lambda batch: []  # every batch unreadable
    sup = runs_module.RunSupervisor(
        pipeline=pipeline, log=events_module.EventLog(), max_gate_rounds=10
    )

    sup.run()

    assert sup.state.gate.rounds == 1, "one round, then stop - not ten"


def test_scored_counts_accumulate_across_rounds():
    pipeline = FakePipeline(gate_rounds=3)
    sup = supervisor(pipeline)

    sup.run()

    assert sup.state.gate.scored == 12, "four jobs a round, three rounds"


def test_the_plan_reports_every_job_gated_across_the_run():
    pipeline = FakePipeline(gate_rounds=3)
    sup = supervisor(pipeline)

    sup.run()

    assert sup.state.gate.plan.jobs == 12


def test_a_stop_lands_mid_loop_and_does_not_start_another_round():
    pipeline = FakePipeline(gate_rounds=10)
    sup = supervisor(pipeline)
    original = pipeline.gate

    def stop_after_first(batch):
        sup.request_stop()
        return original(batch)

    pipeline.gate = stop_after_first
    sup.run()

    assert sup.state.outcome == "killed"
    assert sup.state.gate.rounds <= 1


class PrefetchingPipeline(FakePipeline):
    """A pipeline that fetches every source up front, as the engine does."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prefetched: list[list[str]] = []

    def prefetch(self, sources):
        self.prefetched.append(list(sources))
        # Finished in reverse: storing follows the fetches, not the list.
        for source in reversed(sources):
            yield source, "ConnectError: down" if source == "ashby" else None


def test_each_source_is_stored_in_the_order_its_fetch_finished():
    pipeline = PrefetchingPipeline(sources=["greenhouse", "ashby"], boards=1)
    sup = supervisor(pipeline)
    sup.run()

    assert pipeline.prefetched == [["greenhouse", "ashby"]]
    assert [source for source, _ in pipeline.fetched] == ["ashby", "greenhouse"]
    messages = [event.message for event in sup.log]
    assert "fetching 2 sources at once" in messages
    assert any("ashby fetch failed ahead of time" in message for message in messages)
    assert sup.state.outcome == "completed"


def test_a_resumed_run_fetches_up_front_only_what_it_had_not_finished():
    pipeline = PrefetchingPipeline(sources=["greenhouse", "ashby"], boards=1)
    sup = supervisor(pipeline)
    pipeline.on_fetch = lambda count: sup.request_pause() if count == 1 else None
    sup.run()
    pipeline.on_fetch = None
    sup.run_resumed()

    assert pipeline.prefetched == [["greenhouse", "ashby"], ["greenhouse"]]

