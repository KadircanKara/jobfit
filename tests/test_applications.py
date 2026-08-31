"""apply, skip, and the state machine. Nothing here contacts an employer.

PLAN.md non-negotiable 1: no auto-apply, ever. These tests exist partly to keep
that honest, and partly because the apply path is the one place a truncated JD
could reach the tailoring skill.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from sqlalchemy import select

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
    """The skill's own scripts cd into "<Company> - <Position> @ <Location>".
    jobhunt conforms to the tool that already works rather than the other way
    round."""
    assert (
        tailoring.folder_name("Acme", "Senior Backend Engineer", "Istanbul, Turkey")
        == "Acme - Senior Backend Engineer @ Istanbul, Turkey"
    )


def test_a_posting_with_no_location_keeps_the_two_part_name() -> None:
    """An empty suffix would be worse than no suffix."""
    assert tailoring.folder_name("Acme", "Engineer") == "Acme - Engineer"
    assert tailoring.folder_name("Acme", "Engineer", "N/A") == "Acme - Engineer"
    assert tailoring.folder_name("Acme", "Engineer", "  ") == "Acme - Engineer"


def test_a_repeated_location_is_said_once() -> None:
    """"Singapore, Singapore, Singapore" is a real value from a real board."""
    name = tailoring.folder_name("Acme", "Engineer", "Singapore, Singapore, Singapore")

    assert name == "Acme - Engineer @ Singapore"


def test_a_location_with_slashes_stays_one_folder() -> None:
    """"São Paulo / SP / Brasil" unstripped would make three nested directories."""
    name = tailoring.folder_name("Acme", "Engineer", "São Paulo / SP / Brasil")

    assert "/" not in name
    assert name.startswith("Acme - Engineer @ São Paulo")


def test_the_separator_survives_a_hyphenated_company(cfg) -> None:
    """" - " stays the separator so a hyphen inside a name does not read as one."""
    name = tailoring.folder_name("Île-de-France GmbH", "Engineer", "Paris, France")

    assert name.split(" - ") == ["Île-de-France GmbH", "Engineer @ Paris, France"]


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
    """A cached score below the gate is never trusted outright (it may be stale -
    see the recompute in `applications.apply`), so this uses a description that
    genuinely re-scores low rather than relying on the stale 0.3 alone."""
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg, description_text="Two lines.", description_md="Two lines.")
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


def test_a_stale_failing_score_is_recomputed_not_trusted(cfg, tmp_path) -> None:
    """A helper change to how description_text is built (e.g. the html_to_text
    paragraph-break fix) can make a JD that used to score below the gate score
    above it today. A cached failing score must not permanently refuse it -
    only a passing score is left alone."""
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)  # GOOD_JD: genuinely scores above the gate
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.1  # stale, pre-fix verdict
    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id)
    assert result.jd_quality is not None and result.jd_quality >= 0.5
    with session_scope(cfg.db_path) as session:
        assert session.get(Job, job_id).jd_quality_score >= 0.5


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


# --- moving existing folders onto the scheme ------------------------------------


def rename_setup(cfg, tmp_path, folder_name, *, location="Istanbul, Turkey", recorded=False):
    """One posting in the corpus and one folder on disk, named the old way."""
    root = apply_root(cfg, tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    folder = root / folder_name
    folder.mkdir()
    (folder / "cv.tex").write_text("x", encoding="utf-8")

    job_id = make_job(cfg, location_raw=location)
    if recorded:
        with session_scope(cfg.db_path) as session:
            session.add(Application(job_id=job_id, status="applied", folder_path=str(folder)))
    return root, folder, job_id


def test_a_folder_is_placed_by_its_recorded_application(cfg, tmp_path) -> None:
    root, folder, _ = rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer", recorded=True)

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert [p.new for p in plans] == [
        str(root / "Acme - Senior Backend Engineer @ Istanbul, Turkey")
    ]


def test_a_folder_with_no_application_row_is_placed_by_its_own_name(cfg, tmp_path) -> None:
    """Most folders here were made by running the skill by hand, so there is no
    row to look them up by."""
    root, _, _ = rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer")

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert [p.new for p in plans] == [
        str(root / "Acme - Senior Backend Engineer @ Istanbul, Turkey")
    ]


def test_a_folder_that_matches_no_posting_is_reported_not_guessed(cfg, tmp_path) -> None:
    rename_setup(cfg, tmp_path, "Someone Else - A Job Never Scraped")

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert len(plans) == 1
    assert not plans[0].doable
    assert "no posting" in plans[0].reason


def test_a_folder_already_named_correctly_is_left_alone(cfg, tmp_path) -> None:
    rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer @ Istanbul, Turkey")

    with session_scope(cfg.db_path) as session:
        assert applications.plan_renames(cfg, session) == []


def test_a_rename_that_would_collide_is_refused(cfg, tmp_path) -> None:
    root, _, _ = rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer")
    (root / "Acme - Senior Backend Engineer @ Istanbul, Turkey").mkdir()

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert not plans[0].doable
    assert "already exists" in plans[0].reason


def test_a_dry_run_moves_nothing(cfg, tmp_path) -> None:
    _, folder, _ = rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer", recorded=True)

    with session_scope(cfg.db_path) as session:
        applications.rename_folders(cfg, session, dry_run=True)

    assert folder.exists(), "a dry run that renamed anything would be a trap"


def test_the_folder_and_the_recorded_path_move_together(cfg, tmp_path) -> None:
    """A folder renamed without its folder_path is a CV the studio cannot find."""
    root, folder, job_id = rename_setup(
        cfg, tmp_path, "Acme - Senior Backend Engineer", recorded=True
    )

    with session_scope(cfg.db_path) as session:
        applications.rename_folders(cfg, session, dry_run=False)

    moved = root / "Acme - Senior Backend Engineer @ Istanbul, Turkey"
    assert moved.is_dir() and not folder.exists()
    assert (moved / "cv.tex").read_text() == "x", "contents come with it"
    with session_scope(cfg.db_path) as session:
        row = session.scalars(select(Application).where(Application.job_id == job_id)).one()
        assert row.folder_path == str(moved)


def test_a_second_run_has_nothing_left_to_do(cfg, tmp_path) -> None:
    rename_setup(cfg, tmp_path, "Acme - Senior Backend Engineer", recorded=True)

    with session_scope(cfg.db_path) as session:
        applications.rename_folders(cfg, session, dry_run=False)
    with session_scope(cfg.db_path) as session:
        assert applications.plan_renames(cfg, session) == []


def test_a_rename_never_downgrades_the_name_already_on_disk(cfg, tmp_path) -> None:
    """Boards spell themselves in lower case. A folder correctly named "Apify"
    must not become "apify" just because the corpus says so."""
    root = apply_root(cfg, tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "Apify - AI Engineer").mkdir()
    make_job(cfg, company_name="apify", title="AI Engineer", location_raw="Prague")

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert [pathlib.Path(p.new).name for p in plans] == ["Apify - AI Engineer @ Prague"]


def test_one_title_in_several_cities_is_left_alone(cfg, tmp_path) -> None:
    """Guessing would put the wrong city on a real application folder."""
    root = apply_root(cfg, tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "openai - Applied AI Engineer").mkdir()
    make_job(cfg, external_id="a", company_name="openai",
             title="Applied AI Engineer", location_raw="Delhi, India")
    make_job(cfg, external_id="b", company_name="openai",
             title="Applied AI Engineer", location_raw="London, United Kingdom")

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert not plans[0].doable
    assert "locations differ" in plans[0].reason


def test_a_folder_that_already_has_a_location_is_not_double_suffixed(cfg, tmp_path) -> None:
    root = apply_root(cfg, tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "Acme - Engineer @ Berlin").mkdir()
    make_job(cfg, company_name="Acme", title="Engineer", location_raw="Istanbul, Turkey")

    with session_scope(cfg.db_path) as session:
        plans = applications.plan_renames(cfg, session)

    assert [pathlib.Path(p.new).name for p in plans] == ["Acme - Engineer @ Istanbul, Turkey"]


# --- tailored is not applied ----------------------------------------------------


def test_cutting_a_cv_records_tailored_not_applied(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)

    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, tailor=False, status="tailored")
    with session_scope(cfg.db_path) as session:
        row = session.scalars(select(Application).where(Application.job_id == job_id)).one()
        assert row.status == "tailored"
        assert row.applied_at is None, "nothing was sent, so there is no applied date"


def test_a_tailored_job_stays_on_the_shortlist(cfg, tmp_path) -> None:
    """The job you have a CV for but have not sent is exactly the one you still
    need to see."""
    from jobhunt.render import review

    apply_root(cfg, tmp_path)
    job_id = make_scored_for_shortlist(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, tailor=False, status="tailored")

    assert [card.job_id for card in review.shortlist(cfg, min_score=0.5)] == [job_id]


def test_an_applied_job_leaves_the_shortlist(cfg, tmp_path) -> None:
    from jobhunt.render import review

    apply_root(cfg, tmp_path)
    job_id = make_scored_for_shortlist(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, tailor=False, status="applied")

    assert review.shortlist(cfg, min_score=0.5) == []


def test_a_tailored_folder_with_a_cv_is_not_prepared_twice(cfg, tmp_path) -> None:
    """Two different questions: the shortlist wants the job back, the folder
    guard still has to refuse overwriting the CV already cut."""
    root = apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id, tailor=False, status="tailored")
    (pathlib.Path(result.folder) / "Kadircan_Kara-CV.pdf").write_bytes(b"%PDF-1.4 cut")
    assert root.is_dir()

    with session_scope(cfg.db_path) as session:
        with pytest.raises(applications.ApplyBlocked, match="already recorded as tailored"):
            applications.apply(cfg, session, job_id, tailor=False, status="tailored")


def test_a_tailored_folder_without_a_cv_is_prepared_again(cfg, tmp_path) -> None:
    """Found live: a run that died after prepare() left a folder with no CV in
    it, and the folder alone was enough to skip the job forever."""
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        first = applications.apply(cfg, session, job_id, tailor=False, status="tailored")
    assert applications.tailored_cv(first.folder) is None

    with session_scope(cfg.db_path) as session:
        again = applications.apply(cfg, session, job_id, tailor=False, status="tailored")
    assert again.folder == first.folder


def test_an_empty_cv_pdf_does_not_count_as_compiled(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        result = applications.apply(cfg, session, job_id, tailor=False, status="tailored")
    (pathlib.Path(result.folder) / "Kadircan_Kara_CV.pdf").write_bytes(b"")

    assert applications.tailored_cv(result.folder) is None
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, tailor=False, status="tailored")


def test_tailored_moves_on_to_applied(cfg, tmp_path) -> None:
    apply_root(cfg, tmp_path)
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        applications.apply(cfg, session, job_id, tailor=False, status="tailored")

    with session_scope(cfg.db_path) as session:
        row = applications.advance(session, job_id, "applied")
        assert row.status == "applied"


def make_scored_for_shortlist(cfg) -> int:
    """A job that would reach the shortlist if nothing excluded it."""
    job_id = make_job(cfg)
    with session_scope(cfg.db_path) as session:
        session.add(Score(
            job_id=job_id, profile="global_remote", deterministic_pass=True,
            deterministic_notes={"passed": True, "boost": 1.0},
            llm_score=0.9, llm_reasoning="strong",
        ))
    return job_id


# --- applying to a job whose CV was already cut ---------------------------------
#
# The whole point of keeping `tailored` out of SETTLED is that a job you have
# prepared but not sent stays on the shortlist. That only works if it can later
# be moved to `applied`. Until now it could not: the guard refused any status in
# ACTED_ON, and `tailored` is in ACTED_ON, so recording an application against a
# tailored job silently did nothing while the CSV said otherwise.


def _job_with_status(session, status: str) -> int:
    job = Job(
        external_id=f"ext-{status}",
        source="wwr",
        market="global_remote",
        title="Software Engineer",
        title_normalized="software engineer",
        is_active=True,
    )
    session.add(job)
    session.flush()
    session.add(Application(job_id=job.id, status=status, folder_path="/tmp/cut"))
    return job.id


def test_a_tailored_job_can_be_marked_applied(cfg) -> None:
    with session_scope(cfg.db_path) as session:
        job_id = _job_with_status(session, "tailored")

    with session_scope(cfg.db_path) as session:
        row = applications.record_applied(session, job_id)

        assert row.status == "applied", (
            "cutting a CV must not be the thing that stops you recording the application"
        )


def test_marking_applied_stamps_the_time(cfg) -> None:
    with session_scope(cfg.db_path) as session:
        job_id = _job_with_status(session, "tailored")

    with session_scope(cfg.db_path) as session:
        assert applications.record_applied(session, job_id).applied_at is not None


def test_a_job_already_further_along_is_not_dragged_back_to_applied(cfg) -> None:
    """The guard's real job: re-applying must not clobber pipeline state."""
    with session_scope(cfg.db_path) as session:
        job_id = _job_with_status(session, "interview")

    with session_scope(cfg.db_path) as session:
        assert applications.record_applied(session, job_id).status == "interview"


def test_a_skipped_job_stays_skipped(cfg) -> None:
    with session_scope(cfg.db_path) as session:
        job_id = _job_with_status(session, "skipped")

    with session_scope(cfg.db_path) as session:
        assert applications.record_applied(session, job_id).status == "skipped"


def test_tailored_is_deliberately_not_a_settled_status() -> None:
    """Pins the line the whole feature rests on.

    A job whose CV is cut but never sent is exactly the one that must keep
    showing up. Folding ACTED_ON and SETTLED into one set would look like a
    tidy-up and would quietly hide every tailored job.
    """
    assert "tailored" in applications.ACTED_ON
    assert "tailored" not in applications.SETTLED
