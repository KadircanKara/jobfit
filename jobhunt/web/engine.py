"""The real pipeline: the supervisor's Protocol, backed by the engine.

Deliberately thin. Everything here is a call into code the CLI already uses, so
a run started from the browser and a run started from the terminal do the same
work in the same order.
"""
from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

from jobhunt import preferences as prefs_module
from jobhunt import sources as source_registry
from jobhunt import sync
from jobhunt.config import Config
from jobhunt.rank import deterministic
from jobhunt.rank import runner as rank_runner
from jobhunt.web import fx as fx_module
from jobhunt.web.gate import Gate
from jobhunt.web.runs import DropReason, GateBatch, GatePlan, RankReport

# How many jobs the bar turned away are kept on screen. Enough to show that a
# thin run is a threshold problem, few enough that the shortlist still reads as
# a shortlist.
NEAR_MISSES = 6


class EnginePipeline:
    """Adapts sync / rank / gate / shortlist to what RunSupervisor calls."""

    def __init__(self, config: Config, *, gate: Gate | None = None) -> None:
        self.config = config
        self.gate_runner = gate or Gate(config=config)
        self.on_board: Callable[[str, int, int, str], None] | None = None
        # Called when a source finished but did not do the whole job. Separate
        # from an exception because a degraded source still returns a count.
        self.on_degraded: Callable[[str, str | None], None] | None = None
        # Called with a partial RankReport while stage 1 runs, so a pass over
        # thousands of jobs is not a blank panel until it finishes.
        self.on_rank: Callable[[RankReport], None] | None = None
        self.should_stop: Callable[[], bool] = lambda: False
        # One snapshot per run, taken when ranking starts. See fx.load.
        self.rates: fx_module.Snapshot | None = None
        self._batch_path = pathlib.Path(
            str(config.get("ranking", "batch_path", default=config.home / "data/rank/batch.json"))
        )

    def sources(self) -> list[str]:
        """Only what the search selected. A run that fetches thirteen corpora is
        not a way to see what one of them is worth."""
        prefs, _ = prefs_module.load(self.config)
        selected = prefs_module.adapters_for(prefs.sources)
        return [name for name in sorted(source_registry.REGISTRY) if name in set(selected)]

    # --- sync ------------------------------------------------------------

    def fetch_source(self, source: str) -> sync.SourceResult:
        """One source, end to end. Never raises: failures ride on the result."""
        def progress(done: int, total: int, token: str) -> None:
            if self.on_board:
                self.on_board(source, done, total, token)

        return sync.sync_source(
            self.config,
            source,
            progress=progress,
            should_stop=self.should_stop,
        )

    # --- the supervisor's Protocol ---------------------------------------
    # The supervisor drives boards itself for sources it can enumerate; for the
    # engine, a source is the unit of work and progress arrives through the
    # hook above, so a source is presented as a single board.

    def boards_for(self, source: str) -> list[str]:
        return [source]

    def fetch_board(self, source: str, board: str) -> int:
        result = self.fetch_source(source)
        # "failed" and "degraded" are the words `sync_source` actually uses.
        # This compared against "error", which it never sets, so every failure
        # returned a job count like a healthy fetch: a run whose every
        # ref timed out finished `completed` with an empty shortlist and no
        # indication anything had gone wrong.
        if result.status == "failed":
            raise RuntimeError(result.error_detail or f"{source} failed")
        if result.status == "degraded" and self.on_degraded:
            # Degraded fetched *something*, so raising would throw away a real
            # count over a partial problem. It still has to be said out loud.
            self.on_degraded(source, result.error_detail)
        return result.new + result.updated

    def rank(self) -> RankReport:
        self.rates = fx_module.load(self.config)
        result = rank_runner.run_deterministic(
            self.config,
            rates=self.rates.rates if self.rates.usable else None,
            progress=self._report_rank if self.on_rank else None,
        )
        return self._rank_report(result)

    def _report_rank(self, partial: Any) -> None:
        if self.on_rank:
            self.on_rank(self._rank_report(partial))

    def _rank_report(self, result: Any) -> RankReport:
        """A DeterministicResult, named the way the browser reads it."""
        reasons = [
            DropReason(
                code=code,
                label=deterministic.REASON_LABELS.get(code, code),
                count=count,
                tunable=code in deterministic.TUNABLE_REASONS,
            )
            for code, count in result.reasons.items()
        ]
        reasons.sort(key=lambda reason: (reason.count, reason.code), reverse=True)
        return RankReport(
            corpus=result.corpus,
            scored=result.scored,
            passed=result.passed,
            failed=result.failed,
            skipped=result.skipped,
            by_market=dict(result.by_market),
            reasons=reasons,
            fx_source=self.rates.source if self.rates else None,
            fx_age_hours=(
                round(self.rates.age_hours, 1) if self.rates and self.rates.usable else None
            ),
        )

    def gate_batches(self) -> GatePlan:
        from jobhunt.pipeline import jd_fetch

        emitted = rank_runner.emit(
            self.config,
            self._batch_path,
            fetch_missing=lambda ids: jd_fetch.fill(self.config, ids),
        )
        if not emitted.get("jobs"):
            return GatePlan(held_by_company_cap=emitted.get("held_by_company_cap", 0))
        import json

        payload = json.loads(self._batch_path.read_text(encoding="utf-8"))
        raw = list(payload.get("batches", []))
        bars = {
            batch.get("min_score_to_surface")
            for batch in raw
            if batch.get("min_score_to_surface") is not None
        }
        return GatePlan(
            # One batch per market, which is how `emit` groups them.
            batches=[
                GateBatch(
                    label=batch.get("market") or "unknown",
                    size=len(batch.get("jobs") or []),
                    payload=batch,
                )
                for batch in raw
            ],
            jobs=emitted["jobs"],
            held_by_company_cap=emitted.get("held_by_company_cap", 0),
            # Only stated when every market agrees; two different bars cannot
            # be drawn as one line.
            bar=float(next(iter(bars))) if len(bars) == 1 else None,
        )

    def gate(self, batch: GateBatch) -> list[dict[str, Any]]:
        import json
        import tempfile

        payload = batch.payload or {}
        jobs = payload.get("jobs") or []
        prompt = self._prompt_for(payload)
        verdicts = self.gate_runner.score(prompt=prompt, batch_size=len(jobs))
        if not verdicts:
            # Left ungated on purpose: these jobs keep their place in the
            # corpus and get another chance next run.
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(verdicts, handle)
            path = pathlib.Path(handle.name)
        try:
            rank_runner.ingest(self.config, path, model="claude-code-print")
        finally:
            path.unlink(missing_ok=True)
        return _verdict_rows(verdicts, jobs, batch.label)

    def _prompt_for(self, batch: dict[str, Any]) -> str:
        import json

        return f"{batch.get('prompt', '')}\n\n## Jobs\n\n{json.dumps(batch.get('jobs', []))}"

    def shortlist(self) -> list[dict[str, Any]]:
        """Build the shortlist and upsert the rolling CSV, as `shortlist --export`.

        An export never touches the applied or cv_status columns, so marks made
        in earlier runs survive this.
        """
        from jobhunt.render import csv_export, review

        prefs, _ = prefs_module.load(self.config)
        cards = review.shortlist(self.config, limit=prefs.top_n)
        csv_export.export(self.config, cards)
        rows = [row_from_card(card) for card in cards]
        # The export happens first and from `cards` alone, so a job below the
        # bar can be shown without ever reaching the CSV.
        rows += [
            row_from_card(card) | {"below_bar": True}
            for card in review.near_misses(self.config, limit=NEAR_MISSES)
        ]
        return rows


def row_from_card(card: Any) -> dict[str, Any]:
    """One shortlist row, named the way the browser reads it."""
    posted = getattr(card, "posted_at", None)
    return {
        "job_id": card.job_id,
        "title": card.title,
        "company": card.company,
        "location": card.location,
        "remote_type": card.remote_type,
        "employment_type": card.employment_type,
        "salary": card.salary,
        "fit": card.score,
        "reasoning": card.reasoning,
        "url": card.apply_url,
        "source": card.source,
        "market": card.market,
        "posted_at": posted.isoformat() if posted else None,
        "jd_completeness": card.jd_completeness,
        "below_bar": False,
    }


def _verdict_rows(
    verdicts: list[dict[str, Any]], jobs: list[dict[str, Any]], market: str
) -> list[dict[str, Any]]:
    """Gate verdicts joined to the postings they scored.

    The gate answers with a job id and a score; the batch it was given is the
    only place the title and company still live.
    """
    by_id = {job.get("job_id"): job for job in jobs}
    rows = []
    for verdict in verdicts:
        job = by_id.get(verdict.get("job_id"), {})
        rows.append({
            "job_id": verdict.get("job_id"),
            "title": job.get("title") or f"job {verdict.get('job_id')}",
            "company": job.get("company") or "—",
            "source": job.get("source") or "",
            "market": market,
            "score": verdict.get("score"),
            "reasoning": verdict.get("reasoning") or "",
            "red_flags": list(verdict.get("red_flags") or []),
        })
    return rows
