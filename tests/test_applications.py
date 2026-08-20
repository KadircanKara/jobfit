"""apply, skip, and the state machine. Nothing here contacts an employer.

PLAN.md non-negotiable 1: no auto-apply, ever. These tests exist partly to keep
that honest, and partly because the apply path is the one place a truncated JD
could reach the tailoring skill.
"""
from __future__ import annotations

import json

import pytest

from jobhunt import applications, store
from jobhunt.db.models import Application, Job, Score
from jobhunt.db.session import session_scope
from jobhunt.integrations import tailoring
from jobhunt.sources.base import JobPosting

GOOD_JD = """
About the role

We need a senior backend engineer to own ingestion. You will design APIs and run
Postgres at scale, working directly with the ML team on model serving.

Requirements

Five years of Python. FastAPI, async IO, and comfort owning infrastructure.

Responsibilities

Design services, review code, and mentor two junior engineers. Own the ingestion
pipeline end to end, from the source adapters through to the search index, and
decide what gets built next with the product team.

Nice to have

Experience with Redis, RQ, or another job queue. Prior work on data pipelines at
a company where the data volume actually mattered.
"""


def make_job(cfg, **kwargs) -> int:
    defaults = {
        "source": "ashby", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "description_text": GOOD_JD, "description_md": GOOD_JD,
        "jd_completeness": "full", "jd_source": "api",
        "apply_url": "https://jobs.ashbyhq.com/acme/1",
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        job.jd_quality_score = kwargs.pop("jd_quality_score", 0.95)
        session.flush()
        return job.id


def apply_root(cfg, tmp_path):
    cfg.raw["tailoring"]["applications_root"] = str(tmp_path / "Tailored CVs")
    return tmp_path / "Tailored CVs"


# --- folder naming ------------------------------------------------------------


def test_folder_name_matches_what_the_skill_already_uses() -> None:
    """The skill's own scripts cd into "<Company> - <Position>". jobhunt
    conforms to the tool that already works rather than the other way round."""
    assert tailoring.folder_name("Acme", "Senior Backend Engineer") == "Acme - Senior Backend Engineer"


def test_folder_name_strips_only_what_a_filesystem_cannot_take() -> None:
    name = tailoring.folder_name("Acme / Beta", "Engineer: Platform (Remote)")
    assert "/" not in name
    assert ":" not in name
    assert "Remote" in name  # not slugified, the user reads this


# --- apply --------------------------------------------------------------------


def test_apply_writes_the_contract_files_and_records_the_row(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    job_id = make_job(cfg)

    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id)

    folder = root / "Acme - Senior Backend Engineer"
    assert folder.is_dir()
    assert (folder / "jd.txt").read_text(encoding="utf-8").strip().startswith("About the role")
    sidecar = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    assert sidecar["job_id"] == job_id
    assert sidecar["jd_file"] == "jd.txt"
    assert sidecar["apply_url"].startswith("https://")
    assert result.instruction == f"Tailor my CV for the job in {folder}"
    assert result.mode == "handoff"

    with session_scope(cfg.db_path) as session:
        row = session.query(Application).one()
        assert row.status == "applied"
        assert row.applied_at is not None
        assert row.folder_path == str(folder)


def test_apply_refuses_a_snippet_jd(cfg, tmp_path) -> None:
    """A CV tailored against a truncated JD is worse than no CV: it looks
    finished. This is the one refusal the apply path must make."""
    apply_root(cfg, tmp_path)
    job_id = make_job(
        cfg, description_text="Short teaser.", description_md="Short teaser.",
        jd_completeness="snippet", source_url=None, apply_url=None,
    )
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "--paste" in str(exc.value)


def test_apply_refuses_low_quality_even_when_marked_full(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.3
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "quality" in str(exc.value)


def test_apply_runs_the_ladder_when_the_jd_is_thin(cfg, tmp_path) -> None:
    from jobhunt.extract import fetcher

    apply_root(cfg, tmp_path)
    job_id = make_job(
        cfg, description_text="teaser", description_md="teaser",
        jd_completeness="snippet", source_url="https://example.com/jobs/1",
    )
    payload = {
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": "Senior Backend Engineer", "description": f"<p>{GOOD_JD}</p>",
    }
    path = fetcher.cache_path(cfg, "ashby", job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<html><script type="application/ld+json">{json.dumps(payload)}</script></html>',
        encoding="utf-8",
    )

    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id)
    assert result.extraction is not None
    assert result.jd_source == "jsonld"


def test_apply_never_overwrites_an_existing_folder(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    folder = root / "Acme - Senior Backend Engineer"
    folder.mkdir(parents=True)
    (folder / "Kadircan_Kara-CV.pdf").write_text("previous work", encoding="utf-8")

    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "refusing to overwrite" in str(exc.value)
    assert (folder / "Kadircan_Kara-CV.pdf").read_text(encoding="utf-8") == "previous work"


def test_applying_twice_is_refused(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "already recorded as applied" in str(exc.value)


def test_applying_to_a_job_in_screening_does_not_reset_it(cfg, tmp_path) -> None:
    """Found live: the guard only checked for "applied", so a job advanced to
    screening could be applied to again, silently losing the interview state."""
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)
    with session_scope(cfg.db_path) as session:
        applications.advance(session, job_id, "screening")

    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "already recorded as screening" in str(exc.value)
    with session_scope(cfg.db_path) as session:
        assert session.query(Application).one().status == "screening"


def test_a_skipped_job_is_not_quietly_reopened_by_apply(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.skip(session, job_id, "stack mismatch")
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked):
        applications.apply(cfg, session, job_id)


def test_the_match_reasoning_travels_into_the_sidecar(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(Score(
            job_id=job_id, profile="global_remote", deterministic_pass=True,
            llm_score=0.82, llm_reasoning="FastAPI and LLM orchestration match",
        ))
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)

    sidecar = json.loads(
        (root / "Acme - Senior Backend Engineer" / "job.json").read_text(encoding="utf-8")
    )
    assert sidecar["match_score"] == 0.82
    assert "FastAPI" in sidecar["match_reasoning"]


def test_a_turkish_jd_ships_the_translation_and_keeps_the_original(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    job_id = make_job(cfg, description_lang="tr", description_md="Türkçe ilan metni.")
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).description_en_md = "English rendering of the posting."
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)

    folder = root / "Acme - Senior Backend Engineer"
    assert "English rendering" in (folder / "jd.txt").read_text(encoding="utf-8")
    assert "Türkçe" in (folder / "jd.tr.txt").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, dry_run=True)
    assert not (root / "Acme - Senior Backend Engineer" / "job.json").exists()
    with session_scope(cfg.db_path) as session:
        assert session.query(Application).count() == 0


# --- skip and the state machine ----------------------------------------------


def test_skip_requires_a_reason(cfg) -> None:
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked):
        applications.skip(session, job_id, "   ")


def test_skip_records_the_reason(cfg) -> None:
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.skip(session, job_id, "stack mismatch")
    with session_scope(cfg.db_path) as session:
        row = session.query(Application).one()
        assert (row.status, row.skip_reason) == ("skipped", "stack mismatch")


def test_acting_on_one_source_row_covers_the_whole_cluster(cfg, tmp_path) -> None:
    """The same job on three boards is one job. Applying through one row must
    stop the others resurfacing tomorrow."""
    apply_root(cfg, tmp_path)
    first = make_job(cfg)
    second = make_job(cfg, source="lever", external_id="y1")
    with session_scope(cfg.db_path) as session:
        session.get(Job, first).canonical_job_id = first
        session.get(Job, second).canonical_job_id = first

    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, first)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked):
        applications.apply(cfg, session, second)


def test_status_advances_along_legal_edges(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)
    for step in ("screening", "interview", "offer"):
        with session_scope(cfg.db_path) as session:
            assert applications.advance(session, job_id, step).status == step


def test_status_refuses_an_illegal_jump(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.advance(session, job_id, "applied")
    assert "cannot go applied -> applied" in str(exc.value)


def test_status_needs_an_application_row_first(cfg) -> None:
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.advance(session, job_id, "screening")
    assert "no application row" in str(exc.value)


def test_stale_applications_become_ghosted(cfg, tmp_path) -> None:
    import datetime as dt

    from jobhunt.db.models import utcnow

    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id)
    with session_scope(cfg.db_path) as session:
        session.query(Application).one().applied_at = utcnow() - dt.timedelta(days=45)

    with session_scope(cfg.db_path) as session:
        assert applications.ghost_stale(session, after_days=30) == 1
        assert session.query(Application).one().status == "ghosted"


def test_a_job_the_source_handed_over_complete_needs_no_manual_paste(cfg, tmp_path) -> None:
    """Found live: adapters set jd_completeness "full" and never set a quality
    score, so every API-sourced job hit the quality guard with None."""
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = None

    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id)
    assert result.jd_quality is not None and result.jd_quality >= 0.5

    with session_scope(cfg.db_path) as session:
        assert session.get(Job, job_id).jd_quality_score is not None


def test_a_thin_unscored_jd_is_still_refused(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg, description_text="Two lines.", description_md="Two lines.")
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = None
    with session_scope(cfg.db_path) as session, pytest.raises(applications.ApplyBlocked) as exc:
        applications.apply(cfg, session, job_id)
    assert "quality" in str(exc.value)


def test_response_rate_counts_only_progressed_applications(cfg, tmp_path) -> None:
    """Skips are not applications, and an application with no reply is the
    denominator, not an omission."""
    apply_root(cfg, tmp_path)
    first = make_job(cfg, external_id="a")
    second = make_job(cfg, external_id="b", title="Staff Backend Engineer")
    third = make_job(cfg, external_id="c", title="Platform Engineer")

    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, first)
        applications.apply(cfg, session, second)
        applications.skip(session, third, "not interested")
    with session_scope(cfg.db_path) as session:
        applications.advance(session, first, "screening")

    with session_scope(cfg.db_path) as session:
        rows = {row.status: row for row in session.query(Application).all()}
        assert set(rows) == {"screening", "applied", "skipped"}
