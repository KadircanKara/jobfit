"""The adapter between the supervisor and the real engine.

It exists so the supervisor can stay testable without a network, and so the
engine keeps its own vocabulary rather than growing web concepts.
"""
from __future__ import annotations

import pytest

from jobhunt import preferences as prefs_module
from jobhunt import sources as source_registry
from jobhunt.web import engine as engine_module


def test_the_sources_are_the_registered_ones(cfg):
    pipeline = engine_module.EnginePipeline(cfg)

    assert "greenhouse" in pipeline.sources()
    assert "ashby" in pipeline.sources()


def test_the_engine_fetches_only_from_selected_sources(cfg) -> None:
    """Selecting linkedin alone must exclude the twelve ats adapters, and include
    only linkedin - not a silent fall-back to every ats adapter."""
    prefs, _ = prefs_module.load(cfg)
    prefs.sources = ["linkedin"]
    prefs_module.save(cfg, prefs)
    assert engine_module.EnginePipeline(cfg).sources() == ["linkedin"]


def test_the_engine_fetches_every_ats_adapter_by_default(cfg) -> None:
    """`adapters_for` names linkedin unconditionally, whether or not an adapter
    for it is registered — so the honest expectation intersects with the
    registry. Today that intersection is the twelve ats adapters; once Task 7
    registers linkedin, it becomes thirteen, and this assertion still holds
    without needing an edit."""
    names = engine_module.EnginePipeline(cfg).sources()
    assert "greenhouse" in names and "ashby" in names
    expected = set(prefs_module.adapters_for(["ats", "linkedin"])) & set(source_registry.REGISTRY)
    assert set(names) == expected


def test_fetching_a_source_reports_boards_through_the_progress_hook(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: _fake_result(source, kw, seen),
    )
    pipeline = engine_module.EnginePipeline(cfg)
    pipeline.on_board = lambda source, done, total, token: seen.append((source, done, total))

    pipeline.fetch_source("greenhouse")

    assert seen and seen[0][0] == "greenhouse"


def test_a_stop_request_reaches_the_fetch_loop(cfg, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: captured.update(kw) or _blank_result(source),
    )
    pipeline = engine_module.EnginePipeline(cfg)
    pipeline.should_stop = lambda: True

    pipeline.fetch_source("greenhouse")

    assert captured["should_stop"]() is True


def test_a_degraded_source_comes_back_as_a_failure_rather_than_an_exception(cfg, monkeypatch):
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: _degraded_result(source),
    )
    pipeline = engine_module.EnginePipeline(cfg)

    result = pipeline.fetch_source("greenhouse")

    assert result.status == "degraded"


def test_a_failed_source_raises_out_of_fetch_board(cfg, monkeypatch):
    """`sync_source` reports "failed", never "error" - the string the guard used
    to compare against. While it did, every hard failure returned a job count
    like a healthy fetch, and the run finished `completed` with nothing in it."""
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: _failed_result(source),
    )
    pipeline = engine_module.EnginePipeline(cfg)

    with pytest.raises(RuntimeError, match="everything timed out"):
        pipeline.fetch_board("greenhouse", "greenhouse")


def test_a_degraded_source_is_announced_rather_than_swallowed(cfg, monkeypatch):
    """Degraded fetched something, so it must not raise and lose the count - but
    the run has to hear about it, or a half-empty sync reads as a whole one."""
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: _degraded_result(source),
    )
    pipeline = engine_module.EnginePipeline(cfg)
    heard = []
    pipeline.on_degraded = lambda source, detail: heard.append((source, detail))

    assert pipeline.fetch_board("greenhouse", "greenhouse") == 0
    assert heard == [("greenhouse", "boom")]


def test_a_healthy_source_announces_nothing(cfg, monkeypatch):
    monkeypatch.setattr(
        engine_module.sync, "sync_source",
        lambda config, source, **kw: _blank_result(source),
    )
    pipeline = engine_module.EnginePipeline(cfg)
    heard = []
    pipeline.on_degraded = lambda source, detail: heard.append(source)

    pipeline.fetch_board("greenhouse", "greenhouse")

    assert heard == []


def _failed_result(source):
    result = _blank_result(source)
    result.status = "failed"
    result.error_detail = "everything timed out"
    return result


def _blank_result(source):
    from jobhunt.sync import SourceResult

    return SourceResult(source=source, run_key="r1")


def _degraded_result(source):
    result = _blank_result(source)
    result.status = "degraded"
    result.error_detail = "boom"
    return result


def _fake_result(source, kw, seen):
    hook = kw.get("progress")
    if hook:
        hook(1, 2, "acme")
    return _blank_result(source)


def test_the_shortlist_comes_back_as_rows_the_browser_can_render(cfg):
    """Catches an import or field rename in the engine, which a mock would hide."""
    pipeline = engine_module.EnginePipeline(cfg)

    rows = pipeline.shortlist()

    assert rows == []  # empty corpus, but the call path must be real


def test_a_card_becomes_a_row_carrying_company_and_url(cfg):
    from jobhunt.render.review import Card

    card = Card(
        job_id=7, title="AI Engineer", company="Clera", market="global_remote",
        score=0.88, boost=1.0, location="Munich", remote_type="onsite",
        employment_type="full_time", salary=None, posted_at=None,
        reasoning="close match", apply_url="https://jobs.ashbyhq.com/clera/x",
        source="ashby", also_on=[], jd_completeness="full", yc_batch=None, team_size=None,
    )

    row = engine_module.row_from_card(card)

    assert row["company"] == "Clera"
    assert row["url"] == "https://jobs.ashbyhq.com/clera/x"
    assert row["fit"] == 0.88


# --- what the engine reports about rank and the gate ---------------------------


def test_the_rank_report_labels_and_marks_the_tunable_reasons(cfg):
    from jobhunt.rank import runner as rank_runner

    pipeline = engine_module.EnginePipeline(cfg)
    result = rank_runner.DeterministicResult(
        corpus=40, scored=30, passed=12, failed=18, skipped=10,
        by_market={"eu": 12},
        reasons={"salary_below": 11, "tz_overlap": 7},
    )

    report = pipeline._rank_report(result)

    assert report.passed == 12 and report.corpus == 40
    assert [reason.code for reason in report.reasons] == ["salary_below", "tz_overlap"]
    assert report.reasons[0].label == "salary below the floor"
    assert report.reasons[0].tunable, "the Filters panel can move the salary floor"
    assert not report.reasons[1].tunable, "timezone rules live in filters.yaml"


def test_a_rank_pass_reports_itself_through_the_hook(cfg):
    seen = []
    pipeline = engine_module.EnginePipeline(cfg)
    pipeline.on_rank = seen.append

    pipeline.rank()

    assert seen, "an empty corpus still reports once, so the panel is never blank"
    assert seen[-1].scored == 0


def test_an_empty_corpus_produces_a_gate_plan_with_nothing_in_it(cfg):
    pipeline = engine_module.EnginePipeline(cfg)

    plan = pipeline.gate_batches()

    assert plan.batches == []
    assert plan.jobs == 0


def test_verdicts_are_joined_back_to_the_postings_they_scored():
    """The gate answers with an id and a score. The batch it was handed is the
    only place the title still lives."""
    rows = engine_module._verdict_rows(
        [{"job_id": 7, "score": 0.81, "reasoning": "fits", "red_flags": ["thin jd"]}],
        [{"job_id": 7, "title": "AI Engineer", "company": "Clera", "source": "ashby"}],
        "eu",
    )

    assert rows[0]["title"] == "AI Engineer"
    assert rows[0]["company"] == "Clera"
    assert rows[0]["red_flags"] == ["thin jd"]
    assert rows[0]["market"] == "eu"


def test_a_verdict_for_a_job_missing_from_the_batch_still_renders():
    rows = engine_module._verdict_rows([{"job_id": 9, "score": 0.4}], [], "eu")

    assert rows[0]["title"] == "job 9"
    assert rows[0]["company"] == "—"


def test_a_corpus_with_survivors_produces_batches_to_gate(cfg):
    """Regression: `emit` returns a dict, and the guard here once read it with
    `getattr`, which is always the fallback on a dict. `gate_batches` returned
    an empty list on every run, so the browser never gated a single job — and
    said nothing, because an empty plan is also what a fully gated corpus
    looks like."""
    from jobhunt import store
    from jobhunt.db.models import Score
    from jobhunt.db.session import session_scope
    from jobhunt.sources.base import JobPosting

    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(
            source="ashby", external_id="x1", market="global_remote",
            title="Senior Backend Engineer", company_name="Acme",
            remote_type="remote", description_text="We build things. " * 40,
        ))
        session.flush()
        session.add(Score(
            job_id=job.id, profile=job.market, deterministic_pass=True,
            deterministic_notes={"passed": True, "reasons": [], "codes": []},
        ))

    plan = engine_module.EnginePipeline(cfg).gate_batches()

    assert plan.jobs == 1
    assert [batch.label for batch in plan.batches] == ["global_remote"]
    assert plan.batches[0].size == 1
