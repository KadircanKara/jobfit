"""Stage 1 filtering and the stage 2 file protocol. No network, no model call.

The rule these tests exist to protect: an unknown value is never a rejection.
Aggregators drop fields that a company's own board states fully, so filtering on
absence quietly discards exactly the postings the corpus works hardest to get.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from jobhunt import store
from jobhunt.db.models import Job, Score, utcnow
from jobhunt.db.session import session_scope
from jobhunt.rank import deterministic, profile, runner, timezones
from jobhunt.sources.base import JobPosting

FILTERS = {
    "profiles": {
        "global_remote": {
            "hard_requires": {"remote_type": ["remote", "hybrid"]},
            "hard_excludes": ["us only", "must reside in"],
            "timezone": {"base": "Europe/Istanbul", "min_overlap_hours": 4},
            "seniority_min": "mid",
            "llm_gate_prompt": "prompts/remote_fit.md",
            "min_score_to_surface": 0.7,
        },
        "yc": {
            "seniority_min": "mid",
            "boost": {"founding_engineer": 1.4, "tag_ai": 1.2, "recent_batch": 1.3},
            "llm_gate_prompt": "prompts/yc_fit.md",
        },
    },
    "global": {
        "max_age_days": 30,
        "exclude_companies": ["badco"],
        "exclude_titles_regex": ["(?i)sales|recruiter"],
    },
}


def _counts(emitted: dict) -> tuple[int, int]:
    """(batches, jobs), so an added report field does not fail these tests."""
    return emitted["batches"], emitted["jobs"]


def make_job(cfg, **kwargs):
    defaults = {
        "source": "ashby", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "remote_type": "remote", "description_text": "We build things. " * 40,
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        return job.id


def verdict_for(cfg, job_id, filters=FILTERS):
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        company = job.company
        return deterministic.evaluate(job, company, filters)


# --- timezone -----------------------------------------------------------------


def test_us_roles_fail_a_four_hour_istanbul_overlap() -> None:
    assert timezones.overlap_hours("US") < 4
    assert timezones.overlap_hours("CA") < 4


def test_european_roles_pass_comfortably() -> None:
    for country in ("DE", "GB", "NL", "PL", "TR"):
        assert timezones.overlap_hours(country) >= 6


def test_asian_roles_land_where_expected() -> None:
    assert timezones.overlap_hours("IN") >= 4
    assert timezones.overlap_hours("SG") >= 4
    assert timezones.overlap_hours("AU") < 4


def test_unknown_country_is_not_zero_overlap() -> None:
    """None means "no objection". Zero would mean "reject", which is a different
    claim and one the data does not support."""
    assert timezones.overlap_hours(None) is None
    assert timezones.overlap_hours("ZZ") is None


def test_worldwide_markers_are_recognised() -> None:
    assert timezones.is_worldwide("Remote, Worldwide")
    assert timezones.is_worldwide("Anywhere")
    assert not timezones.is_worldwide("Berlin, Germany")


# --- stage 1 ------------------------------------------------------------------


def test_a_matching_job_passes(cfg) -> None:
    job_id = make_job(cfg, country="DE")
    assert verdict_for(cfg, job_id).passed


def test_onsite_fails_the_remote_requirement(cfg) -> None:
    job_id = make_job(cfg, remote_type="onsite", country="DE")
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("remote_type" in reason for reason in verdict.reasons)


def test_unknown_remote_type_is_not_a_rejection(cfg) -> None:
    job_id = make_job(cfg, remote_type="unknown", country="DE")
    assert verdict_for(cfg, job_id).passed


def test_us_only_phrase_in_the_body_fails(cfg) -> None:
    job_id = make_job(cfg, country="DE", description_text="Great role. US only. " * 20)
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("us only" in reason for reason in verdict.reasons)


def test_a_us_job_fails_on_timezone(cfg) -> None:
    job_id = make_job(cfg, country="US", location_raw="New York, United States")
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("timezone" in reason for reason in verdict.reasons)


def test_worldwide_beats_an_unhelpful_country(cfg) -> None:
    """"Remote, Worldwide" with a US head office is still open to GMT+3."""
    job_id = make_job(cfg, country="US", location_raw="Remote, Worldwide")
    verdict = verdict_for(cfg, job_id)
    assert verdict.passed
    assert verdict.tz_overlap_hours == timezones.FULL_OVERLAP


def test_junior_fails_the_seniority_floor(cfg) -> None:
    job_id = make_job(cfg, title="Junior Backend Engineer", country="DE")
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("seniority" in reason for reason in verdict.reasons)


def test_unknown_seniority_is_not_a_rejection(cfg) -> None:
    job_id = make_job(cfg, title="Backend Engineer", country="DE")
    assert verdict_for(cfg, job_id).passed


def test_a_stale_posting_fails(cfg) -> None:
    old = utcnow() - dt.timedelta(days=60)
    job_id = make_job(cfg, country="DE", posted_at=old)
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("older than" in reason for reason in verdict.reasons)


def test_a_job_with_no_date_is_not_stale(cfg) -> None:
    assert verdict_for(cfg, make_job(cfg, country="DE", posted_at=None)).passed


def test_a_sales_title_fails(cfg) -> None:
    job_id = make_job(cfg, title="Senior Sales Engineer", country="DE")
    assert not verdict_for(cfg, job_id).passed


def test_a_blocklisted_company_fails(cfg) -> None:
    job_id = make_job(cfg, company_name="BadCo", country="DE")
    verdict = verdict_for(cfg, job_id)
    assert not verdict.passed
    assert any("blocklisted" in reason for reason in verdict.reasons)


def test_a_broken_regex_in_the_config_does_not_take_the_run_down(cfg) -> None:
    filters = {"profiles": {}, "global": {"exclude_titles_regex": ["(unclosed"]}}
    assert verdict_for(cfg, make_job(cfg), filters).passed


def test_a_job_from_an_unselected_source_is_dropped(cfg) -> None:
    job_id = make_job(cfg, source="ashby", external_id="src1")
    filters = {**FILTERS, "global": {**FILTERS["global"], "sources": ["linkedin"]}}
    verdict = verdict_for(cfg, job_id, filters)
    assert not verdict.passed
    assert "source_excluded" in verdict.codes


def test_a_job_from_a_selected_source_survives(cfg) -> None:
    job_id = make_job(cfg, source="linkedin", external_id="src2")
    filters = {**FILTERS, "global": {**FILTERS["global"], "sources": ["linkedin"]}}
    assert "source_excluded" not in verdict_for(cfg, job_id, filters).codes


def test_no_source_key_restricts_nothing(cfg) -> None:
    job_id = make_job(cfg, source="ashby", external_id="src3")
    assert "source_excluded" not in verdict_for(cfg, job_id).codes


def test_an_empty_source_list_restricts_everything(cfg) -> None:
    """Present-and-empty is a selection of nothing, not the absence of a rule.
    Reading it as "unrestricted" made the fetch pass and the shortlist disagree:
    the run fetched no source at all while the shortlist kept serving all of them."""
    job_id = make_job(cfg, source="ashby", external_id="src4")
    filters = {**FILTERS, "global": {**FILTERS["global"], "sources": []}}
    assert "source_excluded" in verdict_for(cfg, job_id, filters).codes


def test_changing_the_source_selection_changes_the_fingerprint() -> None:
    base = {"global": {"max_age_days": 30}}
    narrowed = {"global": {"max_age_days": 30, "sources": ["linkedin"]}}
    assert runner.filters_fingerprint(base) != runner.filters_fingerprint(narrowed)


def test_boosts_multiply_but_never_rescue(cfg) -> None:
    job_id = make_job(cfg, market="yc", title="Founding AI Engineer", country="DE")
    verdict = verdict_for(cfg, job_id)
    assert verdict.passed
    assert verdict.boost == pytest.approx(1.4 * 1.2)

    failed = make_job(cfg, market="yc", external_id="x2", title="Founding Sales Engineer")
    assert not verdict_for(cfg, failed).passed


# --- the runner ---------------------------------------------------------------


def test_run_deterministic_writes_scores_and_is_idempotent(cfg) -> None:
    make_job(cfg, country="DE")
    make_job(cfg, external_id="x2", country="US", location_raw="Austin, United States")

    first = runner.run_deterministic(cfg)
    assert first.scored == 2
    assert (first.passed, first.failed) == (1, 1)

    second = runner.run_deterministic(cfg)
    assert second.scored == 0
    assert second.skipped == 2

    with session_scope(cfg.db_path) as session:
        rows = session.query(Score).all()
        assert len(rows) == 2
        assert all(row.deterministic_notes is not None for row in rows)


def test_rescore_reruns_stage_one(cfg) -> None:
    make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    assert runner.run_deterministic(cfg, rescore=True).scored == 1


def test_timezone_overlap_is_persisted_on_the_job(cfg) -> None:
    job_id = make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    with session_scope(cfg.db_path) as session:
        assert session.get(Job, job_id).tz_min_overlap_h >= 6


def test_only_canonical_rows_are_scored(cfg) -> None:
    """Scoring duplicates would multiply the gate batch by the number of sources
    a job happens to appear on."""
    first = make_job(cfg, country="DE")
    second = make_job(cfg, source="lever", external_id="y1", country="DE")
    with session_scope(cfg.db_path) as session:
        session.get(Job, second).canonical_job_id = first

    assert runner.run_deterministic(cfg).scored == 1


# --- the file protocol --------------------------------------------------------


def test_emit_writes_a_self_contained_batch(cfg, tmp_path) -> None:
    make_job(cfg, country="DE")
    runner.run_deterministic(cfg)

    path = tmp_path / "batch.json"
    written = runner.emit(cfg, path)
    assert _counts(written) == (1, 1)

    payload = json.loads(path.read_text(encoding="utf-8"))
    batch = payload["batches"][0]
    assert batch["market"] == "global_remote"
    # The prompt travels with the batch, with the profile already substituted.
    assert "Fit gate" in batch["prompt"]
    assert "{profile}" not in batch["prompt"]
    assert payload["candidate_profile"]
    assert batch["jobs"][0]["title"] == "Senior Backend Engineer"
    assert "job_id" in payload["response_contract"]["item"]


def test_emit_skips_jobs_that_failed_stage_one(cfg, tmp_path) -> None:
    make_job(cfg, country="US", location_raw="Austin, United States")
    runner.run_deterministic(cfg)
    assert _counts(runner.emit(cfg, tmp_path / "b.json")) == (0, 0)


def test_emit_skips_jobs_already_gated(cfg, tmp_path) -> None:
    make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    path = tmp_path / "b.json"
    runner.emit(cfg, path)

    verdicts = tmp_path / "v.json"
    job_id = json.loads(path.read_text())["batches"][0]["jobs"][0]["job_id"]
    verdicts.write_text(json.dumps([{"job_id": job_id, "score": 0.8, "reasoning": "fits"}]))
    runner.ingest(cfg, verdicts)

    assert _counts(runner.emit(cfg, tmp_path / "b2.json")) == (0, 0)


def test_emit_truncates_long_descriptions_and_says_so(cfg, tmp_path) -> None:
    make_job(cfg, country="DE", description_text="word " * 5000)
    runner.run_deterministic(cfg)
    path = tmp_path / "b.json"
    runner.emit(cfg, path)
    job = json.loads(path.read_text())["batches"][0]["jobs"][0]
    assert job["description_truncated"] is True
    assert len(job["description"]) == runner.DESCRIPTION_CHARS


def test_ingest_writes_scores_and_reasoning(cfg, tmp_path) -> None:
    job_id = make_job(cfg, country="DE")
    runner.run_deterministic(cfg)

    path = tmp_path / "v.json"
    path.write_text(json.dumps([{
        "job_id": job_id, "score": 0.82, "reasoning": "FastAPI and LLM work",
        "red_flags": ["on-call"],
    }]))
    result = runner.ingest(cfg, path)
    assert (result.read, result.written) == (1, 1)

    with session_scope(cfg.db_path) as session:
        row = session.query(Score).one()
        assert row.llm_score == pytest.approx(0.82)
        assert "FastAPI" in row.llm_reasoning
        assert "on-call" in row.llm_reasoning
        assert row.llm_model == "claude-code"


def test_ingest_accepts_the_envelope_shape_too(cfg, tmp_path) -> None:
    job_id = make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"batches": [{"verdicts": [
        {"job_id": job_id, "score": 0.5, "reasoning": "ok"}
    ]}]}))
    assert runner.ingest(cfg, path).written == 1


def test_ingest_rejects_a_hallucinated_job_id(cfg, tmp_path) -> None:
    """Writing a score against an id that is not in the corpus would be worse
    than losing the verdict."""
    make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    path = tmp_path / "v.json"
    path.write_text(json.dumps([{"job_id": 999999, "score": 0.9, "reasoning": "x"}]))
    result = runner.ingest(cfg, path)
    assert (result.written, result.unknown) == (0, 1)


def test_ingest_rejects_out_of_range_and_malformed_scores(cfg, tmp_path) -> None:
    job_id = make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    path = tmp_path / "v.json"
    path.write_text(json.dumps([
        {"job_id": job_id, "score": 1.4, "reasoning": "x"},
        {"job_id": job_id, "score": "high", "reasoning": "x"},
        {"job_id": job_id},
    ]))
    result = runner.ingest(cfg, path)
    assert (result.written, result.invalid) == (0, 3)


# --- candidate profile --------------------------------------------------------


def test_profile_strips_latex_to_prose() -> None:
    latex = r"""
\documentclass{article}
\begin{document}
\section{Experience}
\textbf{Senior AI Engineer} at Acme % a comment
FastAPI, Postgres, Redis
\end{document}
"""
    summary = profile.summarize(latex)
    assert "Senior AI Engineer" in summary
    assert "FastAPI, Postgres, Redis" in summary
    assert "\\section" not in summary
    assert "a comment" not in summary


def test_profile_falls_back_when_master_tex_is_missing(cfg, tmp_path) -> None:
    """Ranking has to work on a machine where the CV lives somewhere else."""
    cfg.raw["tailoring"]["master_tex"] = str(tmp_path / "nope.tex")
    assert profile.load(cfg) == profile.FALLBACK


def test_profile_summary_override_wins(cfg) -> None:
    cfg.raw["ranking"]["profile_summary"] = "Backend engineer, Istanbul."
    assert profile.load(cfg) == "Backend engineer, Istanbul."


def test_profile_drops_latex_artifacts() -> None:
    latex = r"""
\begin{document}
\begin{center}
\vspace{5pt}
Kadircan Kara $|$ Senior Engineer
\end{center}
\end{document}
"""
    summary = profile.summarize(latex)
    assert "center" not in summary
    assert "5pt" not in summary
    assert "Kadircan Kara | Senior Engineer" in summary


def test_profile_strips_contact_details() -> None:
    """The gate scores a fit. A phone number and a home email have no business
    in a file written to disk for another tool to read."""
    latex = r"""
\begin{document}
Jane Doe - M.Sc.
+905550000000
mailto:jane@example.com jane@example.com
https://www.linkedin.com/in/jane/ LinkedIn
Istanbul, Turkey
PROFESSIONAL SUMMARY
Senior backend engineer with eight years of Python.
\end{document}
"""
    summary = profile.summarize(latex)
    assert "example.com" not in summary
    assert "linkedin" not in summary.lower()
    assert "+9055" not in summary
    assert "Senior backend engineer" in summary
    assert "Istanbul, Turkey" in summary


def test_a_short_exclusion_does_not_match_inside_a_word(cfg) -> None:
    """Found live: the "W2" exclusion fired on 11 jobs because "w2" turns up
    inside unrelated tokens."""
    filters = {"profiles": {"global_remote": {"hard_excludes": ["w2", "us only"]}}, "global": {}}
    job_id = make_job(cfg, description_text="We run GW2 clusters and aw2 pipelines. " * 20)
    assert verdict_for(cfg, job_id, filters).passed

    real = make_job(cfg, external_id="x9", description_text="Employment is W2 only. " * 20)
    assert not verdict_for(cfg, real, filters).passed


def test_emit_does_not_let_one_company_fill_the_batch(cfg, tmp_path) -> None:
    """Seen live: 11 of a 12 job batch were one translation agency's freelance
    listings. That spends the gate on variations of a single decision."""
    for index in range(8):
        make_job(cfg, external_id=f"acme{index}", company_name="Acme", country="DE")
    for index in range(3):
        make_job(cfg, external_id=f"beta{index}", company_name="Beta", country="DE")
    runner.run_deterministic(cfg)

    path = tmp_path / "b.json"
    runner.emit(cfg, path, limit=6, max_per_company=2)
    jobs = json.loads(path.read_text())["batches"][0]["jobs"]
    companies = [job["company"] for job in jobs]
    assert companies.count("Acme") == 2
    assert companies.count("Beta") == 2


def test_a_title_matching_no_required_pattern_fails(cfg) -> None:
    """Watched live: stage 1 was passing telehealth doctors and a construction
    estimator to the gate. Each cost a gate call to reject."""
    filters = {
        "profiles": {"global_remote": {}},
        "global": {"require_titles_regex": ["(?i)engineer|developer"]},
    }
    doctor = make_job(cfg, title="UK Doctor - Flexible Video Consultations")
    assert not verdict_for(cfg, doctor, filters).passed

    engineer = make_job(cfg, external_id="x2", title="Senior Backend Engineer")
    assert verdict_for(cfg, engineer, filters).passed


def test_a_profile_can_override_the_required_titles(cfg) -> None:
    filters = {
        "profiles": {"global_remote": {"require_titles_regex": ["(?i)designer"]}},
        "global": {"require_titles_regex": ["(?i)engineer"]},
    }
    assert verdict_for(cfg, make_job(cfg, title="Product Designer"), filters).passed
    assert not verdict_for(cfg, make_job(cfg, external_id="x2", title="Engineer"), filters).passed


def test_an_upwork_gig_survives_the_salaried_title_rule(cfg) -> None:
    """The global pattern is built from job titles; a gig is written in prose."""
    job_id = make_job(cfg, source="upwork", market="upwork", external_id="u1",
                       title="Need a Django dev to fix my scraper")
    filters = {
        "profiles": {"upwork": {"require_titles_regex": [".*"]}},
        "global": {"require_titles_regex": ["(?<!\\w)(Backend Engineer)"], "max_age_days": 30},
    }
    assert "title_unmatched" not in verdict_for(cfg, job_id, filters).codes


def test_the_packaged_filters_keep_engineering_titles_and_drop_the_rest(cfg) -> None:
    """A guard on the shipped defaults, not on the mechanism."""
    filters = deterministic.load_filters(cfg)
    keep = ["Senior Backend Engineer", "Staff Software Engineer", "MLOps Engineer",
            "Kıdemli Yazılım Geliştirici", "Head of Engineering", "Data Scientist"]
    drop = ["UK Doctor - Telehealth", "Commercial Construction Estimator",
            "Senior Somali Linguistic QA Tester", "Executive Assistant"]
    for index, title in enumerate(keep):
        job_id = make_job(cfg, external_id=f"k{index}", title=title, country="DE")
        assert verdict_for(cfg, job_id, filters).passed, title
    for index, title in enumerate(drop):
        job_id = make_job(cfg, external_id=f"d{index}", title=title, country="DE")
        assert not verdict_for(cfg, job_id, filters).passed, title


# --- salary floor -------------------------------------------------------------

SALARY_FILTERS = {
    "profiles": {"global_remote": {"salary": {"min_annual": 100000, "currency": "USD"}}},
    "global": {},
}


def test_a_band_whose_top_clears_the_floor_passes(cfg) -> None:
    """80k-120k clears a 100k floor. Comparing the bottom would drop it."""
    job_id = make_job(cfg, salary_min=80000, salary_max=120000, salary_currency="USD",
                      salary_period="annual", salary_is_stated=True)
    assert verdict_for(cfg, job_id, SALARY_FILTERS).passed


def test_a_band_entirely_below_the_floor_fails(cfg) -> None:
    job_id = make_job(cfg, salary_min=50000, salary_max=70000, salary_currency="USD",
                      salary_period="annual", salary_is_stated=True)
    verdict = verdict_for(cfg, job_id, SALARY_FILTERS)
    assert not verdict.passed
    assert any("salary" in reason for reason in verdict.reasons)


def test_a_monthly_band_is_converted_before_comparing(cfg) -> None:
    job_id = make_job(cfg, salary_min=10000, salary_max=12000, salary_currency="USD",
                      salary_period="monthly", salary_is_stated=True)
    assert verdict_for(cfg, job_id, SALARY_FILTERS).passed


def test_another_currency_is_treated_as_unknown_not_converted(cfg) -> None:
    """No FX table on purpose: a rate hardcoded today is wrong in a year and
    would drop jobs with no visible cause."""
    job_id = make_job(cfg, salary_min=60000, salary_max=70000, salary_currency="EUR",
                      salary_period="annual", salary_is_stated=True)
    assert verdict_for(cfg, job_id, SALARY_FILTERS).passed


def test_an_unstated_salary_passes_by_default(cfg) -> None:
    assert verdict_for(cfg, make_job(cfg), SALARY_FILTERS).passed


def test_an_unstated_salary_can_be_excluded_explicitly(cfg) -> None:
    filters = {
        "profiles": {"global_remote": {"salary": {"min_annual": 100000, "currency": "USD",
                                                  "include_unstated": False}}},
        "global": {},
    }
    verdict = verdict_for(cfg, make_job(cfg), filters)
    assert not verdict.passed
    assert "salary not stated" in verdict.reasons


# --- seniority ceiling --------------------------------------------------------


def test_a_role_above_the_ceiling_fails(cfg) -> None:
    """A senior engineer applying to a principal posting wastes a gate call and
    an application."""
    filters = {"profiles": {"global_remote": {"seniority_min": "mid",
                                              "seniority_max": "staff"}}, "global": {}}
    job_id = make_job(cfg, title="Principal Backend Engineer")
    verdict = verdict_for(cfg, job_id, filters)
    assert not verdict.passed
    assert any("above staff" in reason for reason in verdict.reasons)

    ok = make_job(cfg, external_id="x2", title="Staff Backend Engineer")
    assert verdict_for(cfg, ok, filters).passed


# --- employment type ----------------------------------------------------------


def test_employment_type_filters_through_the_existing_hard_requires(cfg) -> None:
    filters = {
        "profiles": {"global_remote": {"hard_requires": {"employment_type": ["full_time"]}}},
        "global": {},
    }
    full = make_job(cfg, employment_type="full_time")
    assert verdict_for(cfg, full, filters).passed

    intern = make_job(cfg, external_id="x2", employment_type="internship")
    assert not verdict_for(cfg, intern, filters).passed

    # Greenhouse and RemoteOK never state it, and those jobs must survive.
    unstated = make_job(cfg, external_id="x3", employment_type=None)
    assert verdict_for(cfg, unstated, filters).passed


def test_regate_includes_jobs_that_already_have_a_verdict(cfg, tmp_path) -> None:
    """Prompts and stated constraints change. A verdict produced under criteria
    the user has since disagreed with must not be frozen in place."""
    job_id = make_job(cfg, country="DE")
    runner.run_deterministic(cfg)
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps([{"job_id": job_id, "score": 0.5, "reasoning": "old"}]))
    runner.ingest(cfg, verdicts)

    assert _counts(runner.emit(cfg, tmp_path / "a.json")) == (0, 0)
    assert _counts(runner.emit(cfg, tmp_path / "b.json", regate=True)) == (1, 1)


def test_regate_puts_the_highest_previous_scores_first(cfg, tmp_path) -> None:
    low = make_job(cfg, external_id="a", country="DE")
    high = make_job(cfg, external_id="b", country="DE", title="Staff Backend Engineer")
    runner.run_deterministic(cfg)
    verdicts = tmp_path / "v.json"
    verdicts.write_text(json.dumps([
        {"job_id": low, "score": 0.3, "reasoning": "x"},
        {"job_id": high, "score": 0.9, "reasoning": "x"},
    ]))
    runner.ingest(cfg, verdicts)

    path = tmp_path / "b.json"
    runner.emit(cfg, path, regate=True)
    ids = [job["job_id"] for job in json.loads(path.read_text())["batches"][0]["jobs"]]
    assert ids[0] == high


def test_stated_constraints_reach_the_gate_prompt(cfg) -> None:
    """A CV says what someone has done. It does not say what they will accept,
    and the gate needs both."""
    cfg.raw.setdefault("ranking", {})["constraints"] = ["Open to relocation."]
    assert "Open to relocation." in profile.load(cfg)
    assert "Stated constraints" in profile.load(cfg)


# --- what a run reports about itself ------------------------------------------


def test_every_drop_reason_carries_a_code_with_a_label(cfg) -> None:
    """The sentence names this job's problem; the code names the family. A
    summary can only count families."""
    job_id = make_job(cfg, remote_type="onsite", country="DE")
    verdict = verdict_for(cfg, job_id)

    assert verdict.codes == ["field_mismatch"]
    assert len(verdict.codes) == len(verdict.reasons)
    assert all(code in deterministic.REASON_LABELS for code in verdict.codes)


def test_codes_are_persisted_with_the_notes(cfg) -> None:
    make_job(cfg, remote_type="onsite", country="DE")
    runner.run_deterministic(cfg)

    with session_scope(cfg.db_path) as session:
        notes = session.scalars(select(Score)).first().deterministic_notes
    assert notes["codes"] == ["field_mismatch"]


def test_a_pass_counts_why_jobs_were_dropped(cfg) -> None:
    make_job(cfg, external_id="a", remote_type="onsite", country="DE")
    make_job(cfg, external_id="b", remote_type="onsite", country="DE")
    make_job(cfg, external_id="c", title="Sales Manager", country="DE")

    result = runner.run_deterministic(cfg)

    assert result.reasons["field_mismatch"] == 2
    assert result.reasons["title_excluded"] == 1
    assert result.corpus == 3


def test_reasons_can_sum_past_the_drop_count(cfg) -> None:
    """A job failing three rules is counted under all three. The UI says so
    rather than hiding it, so the arithmetic has to be the honest one."""
    make_job(cfg, remote_type="onsite", country="US", title="Junior Backend Engineer")

    result = runner.run_deterministic(cfg)

    assert result.failed == 1
    assert sum(result.reasons.values()) > result.failed


def test_a_long_pass_reports_progress_before_it_finishes(cfg) -> None:
    for index in range(runner.PROGRESS_EVERY + 5):
        make_job(cfg, external_id=f"job-{index}", country="DE")
    seen: list[int] = []

    runner.run_deterministic(cfg, progress=lambda partial: seen.append(partial.scored))

    assert len(seen) >= 2, "a pass this long should report at least once mid-way"
    assert seen[0] < seen[-1], "the counts have to move, not repeat"
    assert seen[-1] == runner.PROGRESS_EVERY + 5


def test_a_progress_snapshot_does_not_change_underneath_the_caller(cfg) -> None:
    for index in range(runner.PROGRESS_EVERY + 5):
        make_job(cfg, external_id=f"job-{index}", remote_type="onsite", country="DE")
    snapshots: list[runner.DeterministicResult] = []

    runner.run_deterministic(cfg, progress=snapshots.append)

    first = snapshots[0]
    assert first.scored < snapshots[-1].scored
    assert first.reasons["field_mismatch"] == first.failed


def test_the_company_cap_reports_what_it_held_back(cfg) -> None:
    for index in range(5):
        make_job(cfg, external_id=f"acme-{index}", company_name="Acme", country="DE")
    runner.run_deterministic(cfg)

    emitted = runner.emit(cfg, cfg.home / "batch.json", max_per_company=2)

    assert emitted["jobs"] == 2
    assert emitted["held_by_company_cap"] == 3


# --- where the candidate is ---------------------------------------------------


def test_a_stated_location_wins_over_anything_in_the_cv(cfg) -> None:
    """The seam for a structured profile: set this and nothing is inferred."""
    cfg.raw["ranking"]["candidate_location"] = "Lisbon, Portugal"

    where = profile.location(cfg)

    assert where.text == "Lisbon, Portugal"
    assert where.country == "PT"


def test_a_location_is_read_off_the_address_line_when_none_is_stated() -> None:
    where = profile._location_in("Ada Lovelace\nIstanbul, Turkey\nEXPERIENCE\n")

    assert where.text == "Istanbul, Turkey"
    assert where.country == "TR"


def test_a_sentence_that_merely_names_a_country_contributes_only_the_country() -> None:
    """A whole sentence in the prompt's location slot would drag its own claims
    in with it, so only the country it named survives."""
    line = "- Spent four years shipping payment systems for a bank in Germany, remotely."

    where = profile._location_in(line)

    assert where.country == "DE"
    assert where.text == "Germany"


def test_an_unplaceable_profile_reports_no_location() -> None:
    assert profile._location_in("Ada Lovelace\nEXPERIENCE\nBackend engineer\n") is None


def test_the_country_is_named_as_a_country_not_a_city() -> None:
    """The location rules compare countries. "a country other than Istanbul,
    Turkey" is not a rule a model can apply."""
    where = profile.Location(text="Istanbul, Turkey", country="TR")

    assert where.country_name == "Turkey"


def test_a_gate_prompt_carries_the_location_and_the_country_separately(cfg) -> None:
    cfg.raw["ranking"]["candidate_location"] = "Istanbul, Turkey"
    filters = {"profiles": {"global_remote": {"llm_gate_prompt": "prompts/remote_fit.md"}}}

    text = runner._prompt_text(cfg, filters, "global_remote", "CANDIDATE")

    assert "{location}" not in text and "{country}" not in text
    assert "based in Istanbul, Turkey" in text
    assert "a country other than Turkey" in text


def test_an_unknown_location_tells_the_gate_not_to_score_on_it(cfg) -> None:
    """An assumed home country turns every posting elsewhere into a rejection,
    which is the failure this whole rule exists to stop."""
    cfg.raw["ranking"]["candidate_location"] = None
    cfg.raw["ranking"]["profile_summary"] = "Backend engineer. Python, FastAPI."
    cfg.raw["ranking"]["constraints"] = []
    filters = {"profiles": {"global_remote": {"llm_gate_prompt": "prompts/remote_fit.md"}}}

    text = runner._prompt_text(cfg, filters, "global_remote", "CANDIDATE")

    assert runner.UNKNOWN_LOCATION in text


def test_the_shipped_prompts_only_penalise_a_stated_residency_requirement() -> None:
    """Pins the rule itself: an office abroad is not a penalty, a stated
    requirement to already live abroad is."""
    text = (deterministic.PACKAGED_FILTERS.parent / "prompts/remote_fit.md").read_text()

    assert "must already be" in text
    assert "no location penalty" in text
    assert "The candidate will relocate" in text


# --- a band of one level ------------------------------------------------------
#
# Unclassified seniority normally passes: most postings never state a level, and
# dropping them would empty the corpus. But asking for exactly one level is a
# different request - "internships, nothing else" cannot be honoured while every
# unclassified senior role sails through. So a band of one level, and only that,
# treats an unknown level as a miss.


def test_a_band_of_one_level_refuses_an_unclassified_job(cfg) -> None:
    filters = {"profiles": {"global_remote": {"seniority_min": "intern",
                                              "seniority_max": "intern"}}, "global": {}}
    job_id = make_job(cfg, title="Backend Engineer")  # no level anyone can read

    verdict = verdict_for(cfg, job_id, filters)

    assert not verdict.passed
    assert any("intern" in reason for reason in verdict.reasons)


def test_a_band_of_one_level_keeps_that_level(cfg) -> None:
    filters = {"profiles": {"global_remote": {"seniority_min": "intern",
                                              "seniority_max": "intern"}}, "global": {}}
    job_id = make_job(cfg, title="Backend Engineering Intern")

    assert verdict_for(cfg, job_id, filters).passed


def test_a_band_of_one_level_refuses_a_level_outside_it(cfg) -> None:
    filters = {"profiles": {"global_remote": {"seniority_min": "intern",
                                              "seniority_max": "intern"}}, "global": {}}
    job_id = make_job(cfg, title="Senior Backend Engineer")

    assert not verdict_for(cfg, job_id, filters).passed


def test_a_wider_band_still_passes_an_unclassified_job(cfg) -> None:
    # The lenient default is load-bearing everywhere else and is left alone.
    filters = {"profiles": {"global_remote": {"seniority_min": "junior",
                                              "seniority_max": "senior"}}, "global": {}}
    job_id = make_job(cfg, title="Backend Engineer")

    assert verdict_for(cfg, job_id, filters).passed


def test_a_floor_with_no_ceiling_still_passes_an_unclassified_job(cfg) -> None:
    filters = {"profiles": {"global_remote": {"seniority_min": "intern"}}, "global": {}}
    job_id = make_job(cfg, title="Backend Engineer")

    assert verdict_for(cfg, job_id, filters).passed


def test_a_band_of_one_level_refuses_a_level_it_cannot_place(cfg) -> None:
    # "founding" is a real classification that is not on the ladder at all.
    filters = {"profiles": {"global_remote": {"seniority_min": "intern",
                                              "seniority_max": "intern"}}, "global": {}}
    job_id = make_job(cfg, title="Founding Engineer")

    assert not verdict_for(cfg, job_id, filters).passed


# --- rules that changed since the last pass ------------------------------------
#
# Skipping already-scored jobs is what keeps a repeat run cheap, but it also
# means a verdict outlives the rules that produced it. Narrowing the search to
# internships and widening it again left the internships sitting on the
# shortlist with a passing verdict nothing had revisited. The pass now notices
# that the rules themselves changed and rescores on its own.


def _set_titles(cfg, *titles: str) -> None:
    from jobhunt import preferences

    prefs, _ = preferences.load(cfg)
    prefs.titles = list(titles)
    preferences.save(cfg, prefs)


def test_a_second_pass_still_skips_when_the_rules_have_not_changed(cfg) -> None:
    make_job(cfg, country="DE")
    runner.run_deterministic(cfg)

    second = runner.run_deterministic(cfg)

    assert second.scored == 0
    assert second.skipped == 1


def test_changing_the_rules_rescores_without_being_asked(cfg) -> None:
    make_job(cfg, title="Senior Backend Engineer", country="DE")
    runner.run_deterministic(cfg)

    _set_titles(cfg, "Data Scientist")
    second = runner.run_deterministic(cfg)

    assert second.scored == 1
    assert second.skipped == 0


def test_a_verdict_follows_the_new_rules(cfg) -> None:
    job_id = make_job(cfg, title="Senior Backend Engineer", country="DE")
    runner.run_deterministic(cfg)

    _set_titles(cfg, "Data Scientist")   # the job no longer matches any title
    runner.run_deterministic(cfg)

    with session_scope(cfg.db_path) as session:
        score = session.query(Score).filter_by(job_id=job_id).one()
        assert score.deterministic_pass is False


def test_a_partial_pass_does_not_claim_the_corpus_is_current(cfg) -> None:
    # A --limit or --market pass leaves most of the corpus on the old rules, so
    # it must not record the new fingerprint and suppress the next full rescore.
    make_job(cfg, title="Senior Backend Engineer", country="DE")
    make_job(cfg, external_id="x2", title="Senior Backend Engineer", country="DE")
    runner.run_deterministic(cfg)

    _set_titles(cfg, "Data Scientist")
    runner.run_deterministic(cfg, limit=1)
    after = runner.run_deterministic(cfg)

    assert after.scored == 2
