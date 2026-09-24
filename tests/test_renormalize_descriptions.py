"""scripts/renormalize_descriptions.py: batch loop, empty-HTML skip, dry-run.

No live database here - a tmp_path sqlite database built the same way every
other test does (tests/conftest.build_config), exercising the script's own
`run()` against it.
"""
from __future__ import annotations

import importlib.util
import pathlib

from sqlalchemy import select

from jobhunt import store
from jobhunt.db.models import Job
from jobhunt.db.session import session_scope
from jobhunt.sources.base import JobPosting
from tests.conftest import build_config

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "renormalize_descriptions.py"
_spec = importlib.util.spec_from_file_location("renormalize_descriptions", SCRIPT_PATH)
renormalize = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(renormalize)

RUN_ON_HTML = "<p>First paragraph.</p><p>Second paragraph.</p>"
# The old flattening helper's output for RUN_ON_HTML: no paragraph break.
STALE_TEXT = "First paragraph. Second paragraph."


def make_job(cfg, **kwargs) -> int:
    defaults = {
        "source": "greenhouse", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "jd_completeness": "full", "jd_source": "api",
        "apply_url": "https://job-boards.greenhouse.io/acme/jobs/1",
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        return job.id


def test_regenerates_text_and_refreshes_a_stale_low_score(tmp_path) -> None:
    cfg = build_config(tmp_path)
    job_id = make_job(
        cfg, external_id="a",
        description_html=RUN_ON_HTML, description_text=STALE_TEXT,
    )
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.1  # stale, pre-fix verdict

    renormalize.run(cfg.db_path, batch_size=50, dry_run=False)

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.description_text == "First paragraph.\n\nSecond paragraph."
        assert job.jd_quality_score is not None


def test_skips_rows_with_empty_description_html(tmp_path) -> None:
    cfg = build_config(tmp_path)
    job_id = make_job(
        cfg, external_id="b", source="personio",
        description_html=None, description_text="whatever was there before",
    )
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.2

    renormalize.run(cfg.db_path, batch_size=50, dry_run=False)

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.description_text == "whatever was there before"
        assert job.jd_quality_score == 0.2  # untouched: nothing to regenerate from


def test_dry_run_writes_nothing(tmp_path) -> None:
    cfg = build_config(tmp_path)
    job_id = make_job(
        cfg, external_id="c",
        description_html=RUN_ON_HTML, description_text=STALE_TEXT,
    )
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.1

    renormalize.run(cfg.db_path, batch_size=50, dry_run=True)

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.description_text == STALE_TEXT
        assert job.jd_quality_score == 0.1


def test_processes_more_rows_than_one_batch(tmp_path) -> None:
    """Batch size smaller than the row count: the loop must page through all
    of them, not just the first batch."""
    cfg = build_config(tmp_path)
    for i in range(5):
        make_job(
            cfg, external_id=f"row{i}",
            description_html=RUN_ON_HTML, description_text=STALE_TEXT,
        )

    renormalize.run(cfg.db_path, batch_size=2, dry_run=False)

    with session_scope(cfg.db_path) as session:
        jobs = session.execute(select(Job)).scalars().all()
        assert len(jobs) == 5
        assert all(job.description_text == "First paragraph.\n\nSecond paragraph." for job in jobs)
