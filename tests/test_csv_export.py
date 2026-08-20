"""The rolling CSV. Its one hard rule: an export never touches `applied`.

That is what makes the file safe to regenerate on every run, and it is the only
thing in it that nothing else can reconstruct.
"""
from __future__ import annotations

import csv

from jobhunt import store
from jobhunt.db.models import Score
from jobhunt.db.session import session_scope
from jobhunt.render import csv_export, review
from jobhunt.sources.base import JobPosting


def make_scored(cfg, score_value=0.8, **kwargs) -> int:
    defaults = {
        "source": "ashby", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "description_text": "We build things. " * 40, "remote_type": "remote",
        "employment_type": "full_time", "apply_url": "https://jobs.ashbyhq.com/acme/1",
        "jd_completeness": "full",
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        session.add(Score(
            job_id=job.id, profile=job.market, deterministic_pass=True,
            deterministic_notes={"passed": True, "boost": 1.0},
            llm_score=score_value, llm_reasoning="fits",
        ))
        return job.id


def rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["job_id"]: row for row in csv.DictReader(handle)}


def test_export_writes_the_expected_columns(cfg, tmp_path) -> None:
    make_scored(cfg)
    path = tmp_path / "jobs.csv"
    result = csv_export.export(cfg, review.shortlist(cfg), path)

    assert (result.added, result.total) == (1, 1)
    row = next(iter(rows(path).values()))
    assert row["title"] == "Senior Backend Engineer"
    assert row["company"] == "Acme"
    assert row["work_model"] == "remote"
    assert row["employment_type"] == "full_time"
    assert row["fit"] == "0.80"
    assert row["applied"] == "FALSE"
    assert row["url"].startswith("https://")


def test_applied_survives_a_re_export(cfg, tmp_path) -> None:
    """The rule the whole file depends on."""
    job_id = make_scored(cfg)
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)
    csv_export.mark_applied(path, [job_id], cv_status="cv_ready")

    csv_export.export(cfg, review.shortlist(cfg), path)

    row = rows(path)[str(job_id)]
    assert row["applied"] == "TRUE"
    assert row["cv_status"] == "cv_ready"


def test_a_re_export_refreshes_the_score(cfg, tmp_path) -> None:
    job_id = make_scored(cfg)
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)

    with session_scope(cfg.db_path) as session:
        session.query(Score).filter_by(job_id=job_id).one().llm_score = 0.95

    result = csv_export.export(cfg, review.shortlist(cfg), path)
    assert (result.added, result.refreshed) == (0, 1)
    assert rows(path)[str(job_id)]["fit"] == "0.95"


def test_a_job_that_stops_ranking_stays_in_the_file(cfg, tmp_path) -> None:
    """A job you applied to must not vanish because it dropped off the shortlist."""
    job_id = make_scored(cfg)
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)
    csv_export.mark_applied(path, [job_id])

    csv_export.export(cfg, [], path)
    assert rows(path)[str(job_id)]["applied"] == "TRUE"


def test_new_jobs_append_across_runs(cfg, tmp_path) -> None:
    path = tmp_path / "jobs.csv"
    first = make_scored(cfg, external_id="a")
    csv_export.export(cfg, review.shortlist(cfg), path)
    second = make_scored(cfg, external_id="b", title="Staff Backend Engineer")
    result = csv_export.export(cfg, review.shortlist(cfg), path)

    assert result.added == 1
    assert set(rows(path)) == {str(first), str(second)}


def test_mark_applied_reports_ids_it_does_not_know(cfg, tmp_path) -> None:
    job_id = make_scored(cfg)
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)

    marked, missing = csv_export.mark_applied(path, [job_id, 999999])
    assert marked == [job_id]
    assert missing == [999999]


def test_unapplied_jobs_sort_first_then_by_fit(cfg, tmp_path) -> None:
    low = make_scored(cfg, external_id="a", score_value=0.72)
    high = make_scored(cfg, external_id="b", score_value=0.91, title="Staff Backend Engineer")
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)
    csv_export.mark_applied(path, [high])
    csv_export.export(cfg, review.shortlist(cfg), path)

    with path.open(newline="", encoding="utf-8") as handle:
        order = [row["job_id"] for row in csv.DictReader(handle)]
    assert order == [str(low), str(high)]


def test_no_temp_file_is_left_behind(cfg, tmp_path) -> None:
    make_scored(cfg)
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, review.shortlist(cfg), path)
    assert not path.with_suffix(".csv.tmp").exists()


def test_an_empty_export_creates_a_valid_header_only_file(cfg, tmp_path) -> None:
    path = tmp_path / "jobs.csv"
    csv_export.export(cfg, [], path)
    assert path.exists()
    assert rows(path) == {}
    assert path.read_text(encoding="utf-8").startswith("job_id,fit,title")
