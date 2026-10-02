"""One run at a time, driven from a background thread.

The supervisor owns the phase order and the behaviours that are easy to get
wrong: a single unreachable source must not take the run down, and the two
buttons must mean different things.

**Stop kills.** Nothing after the current unit of work runs — no ranking, no
gating, no shortlist assembled from half a corpus. It is the button for "I
started this by mistake".

**Pause keeps.** The run stops at the same boundary but records which sources it
finished, and resume picks up there. Only sync needs telling where it got to:
ranking skips rows it has already scored, the gate only emits jobs with no
verdict, and the shortlist is a read.

Both land at a checkpoint — between boards and between phases — so a press takes
effect within one unit of work rather than at some arbitrary point mid-write.

It talks to a `pipeline` object rather than to the engine directly, so the
sequencing can be tested without a network.
"""
from __future__ import annotations

import dataclasses
import threading
import traceback
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from jobhunt.web.events import EventLog

# Slices per run. None means no ceiling: the gate keeps going until every job
# that passed the rules has a verdict. A ceiling of 10 (200 jobs) used to leave
# most of a big sync's passes waiting for later runs, and a shortlist built
# from a quarter of them.
DEFAULT_GATE_ROUNDS: int | None = None

PHASES = ("idle", "sync", "rank", "gate", "shortlist", "done", "failed", "paused")


class RunInProgress(RuntimeError):
    """A second run was asked for while one was still going."""


class Killed(RuntimeError):
    """Stop was pressed. Nothing after this point runs."""


class Paused(RuntimeError):
    """Pause was pressed. The run stops where it is and can be picked up."""


@dataclasses.dataclass
class DropReason:
    """One family of stage-1 rejection, with a count for this pass."""

    code: str
    label: str
    count: int
    # True when the Filters panel can move this rule. The rest live in
    # filters.yaml, and pointing at the browser for those would be a lie.
    tunable: bool = False


@dataclasses.dataclass
class RankReport:
    """What stage 1 did, in the shape the browser reads it.

    Built by the pipeline rather than here so the supervisor never has to know
    what a deterministic filter is.
    """

    corpus: int = 0
    scored: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    by_market: dict[str, int] = dataclasses.field(default_factory=dict)
    # Sorted heaviest first. A job failing three rules appears under all three,
    # so these sum past `failed`.
    reasons: list[DropReason] = dataclasses.field(default_factory=list)
    fx_source: str | None = None
    fx_age_hours: float | None = None


@dataclasses.dataclass
class GateBatch:
    """One call to the gate. `payload` is opaque to the supervisor."""

    label: str
    size: int
    payload: Any = None
    status: str = "queued"  # queued | active | done | ungated | skipped


@dataclasses.dataclass
class GatePlan:
    """Everything the gate will do this run, known before the first call."""

    batches: list[GateBatch] = dataclasses.field(default_factory=list)
    jobs: int = 0
    held_by_company_cap: int = 0
    bar: float | None = None


@dataclasses.dataclass
class GateReport:
    plan: GatePlan = dataclasses.field(default_factory=GatePlan)
    # How many slices the run pulled. One means the old behaviour; more means
    # the backlog was being worked through rather than left for the next run.
    rounds: int = 0
    scored: int = 0
    ungated: int = 0
    verdicts: list[dict[str, Any]] = dataclasses.field(default_factory=list)


class Pipeline(Protocol):
    def sources(self) -> list[str]: ...
    # Optional: `prefetch(sources)` yields (source, error) as each source's fetch
    # finishes, so `_sync` stores it then. See EnginePipeline.prefetch.
    def boards_for(self, source: str) -> list[Any]: ...
    def fetch_board(self, source: str, board: Any) -> int: ...
    def rank(self) -> RankReport: ...
    def gate_batches(self) -> GatePlan: ...
    def gate(self, batch: GateBatch) -> list[dict[str, Any]]: ...
    def shortlist(self) -> list[dict[str, Any]]: ...


# A regate can gate far more than one run's worth of jobs. The panel only ever
# shows the most recent verdicts, so the state does not grow without bound.
VERDICT_CAP = 100


@dataclasses.dataclass
class RunState:
    phase: str = "idle"
    outcome: str | None = None
    error: str | None = None
    degraded: list[str] = dataclasses.field(default_factory=list)
    counters: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"boards_done": 0, "jobs_total": 0, "passed": 0}
    )
    rank: RankReport | None = None
    gate: GateReport | None = None
    results: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Sources this run has finished with. A resumed run skips them rather than
    # fetching every board again.
    done_sources: list[str] = dataclasses.field(default_factory=list)
    run_id: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @property
    def running(self) -> bool:
        return self.phase in ("sync", "rank", "gate", "shortlist")

    @property
    def resumable(self) -> bool:
        return self.phase == "paused"


def _job_ids(plan: GatePlan) -> set[Any]:
    """The job ids a slice carries, where the payload says."""
    return {
        job.get("job_id")
        for batch in plan.batches
        for job in ((batch.payload if isinstance(batch.payload, dict) else {}).get("jobs") or [])
        if isinstance(job, dict) and job.get("job_id") is not None
    }


class RunSupervisor:
    def __init__(
        self,
        *,
        pipeline: Pipeline,
        log: EventLog,
        store: Callable[[RunState], None] | None = None,
        clock: Callable[[], str] | None = None,
        max_gate_rounds: int | None = DEFAULT_GATE_ROUNDS,
    ) -> None:
        self.pipeline = pipeline
        self.log = log
        # The gate keeps pulling slices until the backlog is dry. A ceiling, if
        # set, bounds how many model calls one run makes; None means none.
        self.max_gate_rounds = None if max_gate_rounds is None else max(1, int(max_gate_rounds))
        # Called whenever the run reaches a new resting point. Keeps file IO out
        # of here so the sequencing stays testable without a disk.
        self.store = store
        self.clock = clock or (lambda: "")
        self.state = RunState()
        self._kill = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None

    # --- control ---------------------------------------------------------

    def start(self, run_id: str = "", *, fetch: bool = True) -> None:
        """Run in the background so the request that started it can return."""
        if self.state.running:
            raise RunInProgress("a run is already going")
        self._spawn(lambda: self.run(run_id=run_id, fetch=fetch))

    def resume(self) -> None:
        """Pick a paused run back up, keeping what it already did."""
        if self.state.running:
            raise RunInProgress("a run is already going")
        if not self.state.resumable:
            raise RunInProgress("there is no paused run to resume")
        self._spawn(self.run_resumed)

    def _spawn(self, target: Callable[[], Any]) -> None:
        self._thread = threading.Thread(target=target, daemon=True, name="jobhunt-run")
        self._thread.start()

    def request_stop(self) -> None:
        """Kill it. Nothing after the current step runs, and no shortlist is
        built from what it managed to collect."""
        self._kill.set()

    def request_pause(self) -> None:
        self._pause.set()

    @property
    def stopping(self) -> bool:
        return self._kill.is_set()

    @property
    def pausing(self) -> bool:
        return self._pause.is_set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _checkpoint(self) -> None:
        """The only place either button takes effect.

        Checked between phases and between boards, which bounds how long a
        press takes to land at one unit of work rather than at nothing.
        """
        if self._kill.is_set():
            raise Killed()
        if self._pause.is_set():
            raise Paused()

    def _save(self) -> None:
        if self.store is not None:
            self.store(self.state)

    # --- the run ---------------------------------------------------------

    def run(self, run_id: str = "", *, fetch: bool = True) -> RunState:
        """One run. `fetch=False` ranks, gates and shortlists what is already
        stored, for when the corpus is fresh and only the scoring is owed."""
        if self.state.running:
            raise RunInProgress("a run is already going")
        self._kill.clear()
        self._pause.clear()
        self.state = RunState(phase="sync", run_id=run_id, started_at=self.clock())
        if not fetch:
            # Every source counted as done, the same way a resume skips the
            # sources it already fetched.
            self.state.done_sources = list(self.pipeline.sources())
            self.log.emit(phase="sync", message="fetching skipped · scoring what is already stored")
        return self._drive()

    def run_resumed(self) -> RunState:
        """Continue a paused run.

        Only sync needs telling where it got to. Ranking skips rows it has
        already scored, the gate only ever emits jobs with no verdict, and the
        shortlist is a read — so the later phases resume by being run again.
        """
        self._kill.clear()
        self._pause.clear()
        self.state.phase = "sync"
        self.state.outcome = None
        self.log.emit(
            phase="sync",
            message=(
                f"resumed · {len(self.state.done_sources)} sources already fetched"
                if self.state.done_sources
                else "resumed"
            ),
        )
        return self._drive()

    def _drive(self) -> RunState:
        try:
            self._sync()
            self._checkpoint()
            self._rank()
            self._checkpoint()
            self._gate()
            self._checkpoint()
            self._shortlist()
        except Paused:
            self.state.phase = "paused"
            self.state.outcome = None
            self.log.emit(phase="paused", message="paused · resume picks it up here")
            self._save()
            return self.state
        except Killed:
            self.state.phase = "stopped"
            self.state.outcome = "killed"
            self.state.finished_at = self.clock()
            self.log.emit(
                phase="stopped",
                message="stopped · nothing further was run",
                level="warning",
            )
            self._save()
            return self.state
        except Exception:
            self.state.phase = "failed"
            self.state.outcome = "failed"
            self.state.error = traceback.format_exc()
            self.state.finished_at = self.clock()
            self.log.emit(phase="failed", message="the run failed", level="error")
            self._save()
            return self.state

        self.state.phase = "done"
        self.state.outcome = "completed"
        self.state.finished_at = self.clock()
        self.log.emit(
            phase="done",
            message=f"run complete · {len(self.state.results)} jobs shortlisted",
        )
        self._save()
        return self.state

    def _sync(self) -> None:
        self.state.phase = "sync"
        pending = [s for s in self.pipeline.sources() if s not in self.state.done_sources]
        # A pipeline that can fetch every source at once does that, and hands
        # each back as its fetch finishes, so the loop below stores each one
        # while the slower ones are still fetching. One without it fetches in
        # the loop, in order.
        prefetch = getattr(self.pipeline, "prefetch", None)
        order: Iterator[tuple[str, str | None]] = iter((source, None) for source in pending)
        if prefetch is not None and pending:
            self._checkpoint()
            self.log.emit(phase="sync", message=f"fetching {len(pending)} sources at once")
            order = prefetch(pending)
        try:
            self._sync_each(order)
        finally:
            # A stop or pause leaves the loop early; closing ends the fetches
            # still going instead of waiting on them.
            close = getattr(order, "close", None)
            if close is not None:
                close()
        self._save()

    def _sync_each(self, order: Iterator[tuple[str, str | None]]) -> None:
        for source, why in order:
            if why:
                self.log.emit(
                    phase="sync", source=source,
                    message=f"{source} fetch failed ahead of time, retrying on its own · {why}",
                    level="warning",
                )
            self._checkpoint()
            boards = self.pipeline.boards_for(source)
            done = 0
            for board in boards:
                self._checkpoint()
                try:
                    jobs = self.pipeline.fetch_board(source, board)
                except Exception as exc:
                    # One unreachable board is ordinary. Record the source as
                    # degraded and move to the next one rather than ending the
                    # run over it.
                    if source not in self.state.degraded:
                        self.state.degraded.append(source)
                    self.log.emit(
                        phase="sync",
                        source=source,
                        message=f"{source} unreachable · {exc}",
                        level="warning",
                    )
                    break
                done += 1
                self.state.counters["boards_done"] += 1
                self.state.counters["jobs_total"] += jobs
                self.log.emit(
                    phase="sync",
                    source=source,
                    message=f"{source} board {done}/{len(boards)}",
                    boards_done=done,
                    boards_total=len(boards),
                    jobs_new=jobs,
                    jobs_total=self.state.counters["jobs_total"],
                )
            # Recorded after the loop so a source abandoned partway is fetched
            # again on resume rather than silently skipped.
            if source not in self.state.done_sources:
                self.state.done_sources.append(source)

    def _rank(self) -> None:
        self.state.phase = "rank"
        # A long pass reports itself as it goes, through whatever hook the
        # pipeline was wired with. Whatever it managed to report stands until
        # the final report replaces it.
        report = self.pipeline.rank()
        self.state.rank = report
        self.state.counters["passed"] = report.passed
        self.log.emit(
            phase="rank",
            message=(
                f"{report.passed} of {report.scored} jobs passed the deterministic rules"
            ),
        )
        for reason in report.reasons[:3]:
            self.log.emit(phase="rank", message=f"dropped · {reason.label} ({reason.count})")
        self._save()

    def _gate(self) -> None:
        """Work the ungated backlog down, a slice at a time.

        `emit` hands back the jobs that passed the filter and have no verdict
        yet, so asking again after a slice is scored returns the next one. One
        slice per run left everything else waiting for the next press of Start.

        Three things end the loop, four with a round ceiling set, and the third is the one that is easy to
        miss: a slice the gate cannot read keeps its jobs unscored, so the next
        `emit` returns those same jobs. Stopping when a round scores nothing
        keeps that from burning every remaining round on one unreadable slice.
        """
        self.state.phase = "gate"
        report = GateReport()
        self.state.gate = report

        scored_ids: set[Any] = set()
        while self.max_gate_rounds is None or report.rounds < self.max_gate_rounds:
            plan = self.pipeline.gate_batches()
            if not plan.batches:
                if not report.rounds:
                    self.log.emit(phase="gate", message="nothing new to gate")
                break
            # With no round ceiling, a slice that comes back already scored
            # would loop forever: a verdict that does not take the job off the
            # backlog was scored 212 times before this check existed.
            ids = _job_ids(plan)
            if ids and ids <= scored_ids:
                self.log.emit(
                    phase="gate",
                    message=f"{len(ids)} jobs came back after being scored · stopping the gate here",
                    level="warning",
                )
                break
            scored_ids |= ids

            report.rounds += 1
            # The plan on screen is the slice being worked; the job total is
            # what the run has taken on, so a long gate phase reads as progress.
            report.plan = dataclasses.replace(plan, jobs=report.plan.jobs + plan.jobs)
            self.log.emit(
                phase="gate",
                message=(
                    f"round {report.rounds}{f'/{self.max_gate_rounds}' if self.max_gate_rounds else ''} · "
                    f"{plan.jobs} jobs in {len(plan.batches)} batches"
                    + (
                        f" · {plan.held_by_company_cap} held so no company fills a batch"
                        if plan.held_by_company_cap
                        else ""
                    )
                ),
            )

            if not self._gate_round(plan, report):
                self.log.emit(
                    phase="gate",
                    message="nothing scored this round · leaving the rest for next run",
                    level="warning",
                )
                break

    def _gate_round(self, plan: GatePlan, report: GateReport) -> bool:
        """Score one slice. False when nothing came back, which ends the loop."""
        scored_any = False
        for batch in plan.batches:
            # Whatever is already gated stays: a verdict is written per batch,
            # and ungated jobs keep their place in the corpus.
            self._checkpoint()
            batch.status = "active"
            verdicts = self.pipeline.gate(batch) or []
            if verdicts:
                batch.status = "done"
                scored_any = True
                report.scored += len(verdicts)
                report.verdicts = (report.verdicts + list(verdicts))[-VERDICT_CAP:]
                self.log.emit(
                    phase="gate",
                    message=f"{batch.label} · {len(verdicts)} of {batch.size} scored",
                )
            else:
                # The gate gave nothing usable twice. Those jobs keep their
                # place and are gated next run; say so rather than moving on.
                batch.status = "ungated"
                report.ungated += batch.size
                self.log.emit(
                    phase="gate",
                    message=f"{batch.label} · unreadable, left ungated for next run",
                    level="warning",
                )
        return scored_any

    def _shortlist(self) -> None:
        self.state.phase = "shortlist"
        self.log.emit(phase="shortlist", message="checking the postings are still open")
        self.state.results = self.pipeline.shortlist()
        # Near misses ride along in the same list, flagged. Only the jobs above
        # the bar were exported, so only those are counted here.
        kept = sum(1 for row in self.state.results if not row.get("below_bar"))
        missed = len(self.state.results) - kept
        self.log.emit(
            phase="shortlist",
            message=(
                f"{kept} jobs above the bar · CSV written"
                + (f" · {missed} near misses kept on screen" if missed else "")
            ),
        )
        self._save()
