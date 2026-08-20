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
from jobhunt.rank import runner as rank_runner
from jobhunt.web import fx as fx_module
from jobhunt.web.gate import Gate


class EnginePipeline:
    """Adapts sync / rank / gate / shortlist to what RunSupervisor calls."""

    def __init__(self, config: Config, *, gate: Gate | None = None) -> None:
        self.config = config
        self.gate_runner = gate or Gate()
        self.on_board: Callable[[str, int, int, str], None] | None = None
        self.should_stop: Callable[[], bool] = lambda: False
        # One snapshot per run, taken when ranking starts. See fx.load.
        self.rates: fx_module.Snapshot | None = None
        self._batch_path = pathlib.Path(
            str(config.get("ranking", "batch_path", default=config.home / "data/rank/batch.json"))
        )

    @property
    def sources(self) -> list[str]:
        return sorted(source_registry.REGISTRY)

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
        if result.status == "error":
            raise RuntimeError(result.error_detail or f"{source} failed")
        return result.new + result.updated

    def rank(self) -> int:
        self.rates = fx_module.load(self.config)
        result = rank_runner.run_deterministic(
            self.config, rates=self.rates.rates if self.rates.usable else None
        )
        return getattr(result, "passed", 0)

    def gate_batches(self) -> list[dict[str, Any]]:
        emitted = rank_runner.emit(self.config, self._batch_path)
        if not getattr(emitted, "jobs", 0):
            return []
        import json

        payload = json.loads(self._batch_path.read_text(encoding="utf-8"))
        return list(payload.get("batches", []))

    def gate(self, batch: dict[str, Any]) -> None:
        import json
        import tempfile

        prompt = self._prompt_for(batch)
        verdicts = self.gate_runner.score(prompt=prompt, batch_size=len(batch.get("jobs", [])))
        if not verdicts:
            # Left ungated on purpose: these jobs keep their place in the
            # corpus and get another chance next run.
            return
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(verdicts, handle)
            path = pathlib.Path(handle.name)
        try:
            rank_runner.ingest(self.config, path, model="claude-code-print")
        finally:
            path.unlink(missing_ok=True)

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
        return [row_from_card(card) for card in cards]


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
    }
