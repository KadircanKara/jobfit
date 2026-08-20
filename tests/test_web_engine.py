"""The adapter between the supervisor and the real engine.

It exists so the supervisor can stay testable without a network, and so the
engine keeps its own vocabulary rather than growing web concepts.
"""
from __future__ import annotations

from jobhunt.web import engine as engine_module


def test_the_sources_are_the_registered_ones(cfg):
    pipeline = engine_module.EnginePipeline(cfg)

    assert "greenhouse" in pipeline.sources
    assert "ashby" in pipeline.sources


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
