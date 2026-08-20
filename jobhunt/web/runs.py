"""One run at a time, driven from a background thread.

The supervisor owns the phase order and the two behaviours that are easy to get
wrong: a stop request must land within one board fetch and still leave the user
with a shortlist, and a single unreachable source must not take the run down.

It talks to a `pipeline` object rather than to the engine directly, so the
sequencing can be tested without a network.
"""
from __future__ import annotations

import dataclasses
import threading
import traceback
from typing import Any, Protocol

from jobhunt.web.events import EventLog

PHASES = ("idle", "sync", "rank", "gate", "shortlist", "done", "failed")


class RunInProgress(RuntimeError):
    """A second run was asked for while one was still going."""


class Pipeline(Protocol):
    sources: list[str]

    def boards_for(self, source: str) -> list[Any]: ...
    def fetch_board(self, source: str, board: Any) -> int: ...
    def rank(self) -> int: ...
    def gate_batches(self) -> list[Any]: ...
    def gate(self, batch: Any) -> None: ...
    def shortlist(self) -> list[dict[str, Any]]: ...


@dataclasses.dataclass
class RunState:
    phase: str = "idle"
    outcome: str | None = None
    error: str | None = None
    degraded: list[str] = dataclasses.field(default_factory=list)
    counters: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"boards_done": 0, "jobs_total": 0, "passed": 0}
    )
    results: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.phase in ("sync", "rank", "gate", "shortlist")


class RunSupervisor:
    def __init__(self, *, pipeline: Pipeline, log: EventLog) -> None:
        self.pipeline = pipeline
        self.log = log
        self.state = RunState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- control ---------------------------------------------------------

    def start(self) -> None:
        """Run in the background so the request that started it can return."""
        if self.state.running:
            raise RunInProgress("a run is already going")
        self._thread = threading.Thread(target=self.run, daemon=True, name="jobhunt-run")
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # --- the run ---------------------------------------------------------

    def run(self) -> RunState:
        if self.state.running:
            raise RunInProgress("a run is already going")
        self._stop.clear()
        self.state = RunState(phase="sync")
        try:
            self._sync()
            self._rank()
            self._gate()
            self._shortlist()
        except Exception:
            self.state.phase = "failed"
            self.state.outcome = "failed"
            self.state.error = traceback.format_exc()
            self.log.emit(phase="failed", message="the run failed", level="error")
            return self.state

        self.state.phase = "done"
        self.state.outcome = "stopped_early" if self.stopping else "completed"
        self.log.emit(
            phase="done",
            message=(
                f"stopped early · {len(self.state.results)} jobs from a partial corpus"
                if self.stopping
                else f"run complete · {len(self.state.results)} jobs shortlisted"
            ),
        )
        return self.state

    def _sync(self) -> None:
        self.state.phase = "sync"
        for source in list(self.pipeline.sources):
            if self.stopping:
                return
            boards = self.pipeline.boards_for(source)
            done = 0
            for board in boards:
                if self.stopping:
                    return
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

    def _rank(self) -> None:
        self.state.phase = "rank"
        passed = self.pipeline.rank()
        self.state.counters["passed"] = passed
        self.log.emit(phase="rank", message=f"{passed} jobs passed the deterministic rules")

    def _gate(self) -> None:
        self.state.phase = "gate"
        batches = self.pipeline.gate_batches()
        for index, batch in enumerate(batches, start=1):
            if self.stopping and index > 1:
                # Whatever is already gated stays. Ungated jobs keep their
                # place in the corpus and get gated on the next run.
                self.log.emit(phase="gate", message="stopping · leaving the rest ungated")
                return
            self.pipeline.gate(batch)
            self.log.emit(phase="gate", message=f"gated batch {index}/{len(batches)}")

    def _shortlist(self) -> None:
        self.state.phase = "shortlist"
        self.state.results = self.pipeline.shortlist()
        self.log.emit(
            phase="shortlist",
            message=f"{len(self.state.results)} jobs above the bar · CSV written",
        )
