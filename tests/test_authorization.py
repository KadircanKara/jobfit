"""Work authorization and visa sponsorship: reading postings, and the decision."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from jobhunt import store
from jobhunt.db.models import Job, Score
from jobhunt.db.session import session_scope
from jobhunt.pipeline import jd_fetch
from jobhunt.rank import authorization as auth
from jobhunt.rank import deterministic, runner
from jobhunt.sources.base import JobPosting

# --- reading a posting ---------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "required", "sponsorship"),
    [
        ("Candidates must be authorized to work in the United States.", {"US"}, None),
        ("You must be legally authorised to work in the UK without sponsorship.", {"GB"}, "refused"),
        ("Right to work in the UK is required.", {"GB"}, None),
        ("US work authorization required.", {"US"}, None),
        ("This position is open to U.S. citizens and permanent residents only.", {"US"}, None),
        ("Applicants must have a valid work permit for Germany.", {"DE"}, None),
        ("We are unable to sponsor visas at this time.", set(), "refused"),
        ("Visa sponsorship is available for this role.", set(), "offered"),
        ("We offer relocation and visa support.", set(), "offered"),
        ("We are an E-Verify employer and verify employment eligibility.", set(), None),
        ("Must be based in a timezone that overlaps with ours.", set(), None),
        ("We sponsor visas for EU roles but cannot sponsor for US roles.", set(), None),
    ],
)
def test_the_plain_wordings_are_read(sentence, required, sponsorship) -> None:
    facts = auth.extract(sentence)
    assert set(facts.required) == required
    assert facts.sponsorship == sponsorship


def test_a_region_requirement_covers_every_country_in_it() -> None:
    facts = auth.extract("You need to be eligible to work in the EU.")
    assert {"DE", "FR", "IE"} <= facts.required and "TR" not in facts.required


def test_a_two_letter_word_is_not_read_as_a_country() -> None:
    assert auth.extract("You must be authorized to work in it, obviously.").required == frozenset()


def test_the_gate_answer_is_read_and_bad_values_ignored() -> None:
    facts = auth.from_gate({"work_authorization_required": ["United States"], "visa_sponsorship": "refused"})
    assert facts == auth.Facts(required=frozenset({"US"}), sponsorship="refused")
    assert auth.from_gate({"visa_sponsorship": "maybe"}).sponsorship is None
    assert auth.from_gate({"score": 0.5}) is None


def test_a_footer_requirement_reaches_the_gate_past_its_cut() -> None:
    text = "Great role. " * 600 + "Candidates must be authorized to work in Canada."
    assert "authorized to work in Canada" in auth.relevant_lines(text, 6000)
    assert auth.relevant_lines("short", 6000) == ""


# --- the decision --------------------------------------------------------------

TR_ONLY = auth.Candidate(authorized=frozenset({"TR"}), anywhere=False, needs_sponsorship=True)
TR_NO_SPONSOR = auth.Candidate(authorized=frozenset({"TR"}), anywhere=False, needs_sponsorship=False)


def _facts(required=(), sponsorship=None):
    return auth.Facts(required=frozenset(required), sponsorship=sponsorship)


def test_sponsorship_offered_keeps_the_job_whatever_it_requires() -> None:
    assert auth.decide(_facts({"US"}, "offered"), "US", TR_ONLY) is None


def test_a_hard_requirement_elsewhere_drops_the_job() -> None:
    code, reason = auth.decide(_facts({"US"}), "US", TR_ONLY)
    assert code == "auth_required" and "United States" in reason


def test_a_hard_requirement_the_candidate_meets_keeps_it() -> None:
    assert auth.decide(_facts({"TR"}), "TR", TR_ONLY) is None


def test_a_hard_requirement_drops_even_when_sponsorship_is_not_needed() -> None:
    assert auth.decide(_facts({"US"}), "US", TR_NO_SPONSOR)[0] == "auth_required"


def test_no_requirement_keeps_the_job_whatever_the_ticks() -> None:
    assert auth.decide(_facts(), "US", TR_ONLY) is None


def test_refused_sponsorship_drops_only_a_candidate_who_needs_it() -> None:
    assert auth.decide(_facts((), "refused"), "DE", TR_ONLY)[0] == "no_sponsorship"
    assert auth.decide(_facts((), "refused"), "DE", TR_NO_SPONSOR) is None


def test_refused_sponsorship_where_the_candidate_is_authorized_keeps_it() -> None:
    assert auth.decide(_facts((), "refused"), "TR", TR_ONLY) is None


def test_refused_sponsorship_in_an_unknown_country_keeps_it() -> None:
    assert auth.decide(_facts((), "refused"), None, TR_ONLY) is None


def test_authorized_anywhere_keeps_everything() -> None:
    anywhere = auth.Candidate(authorized=frozenset(), anywhere=True, needs_sponsorship=True)
    assert auth.decide(_facts({"US"}, "refused"), "US", anywhere) is None


def test_the_readings_merge_rather_than_overrule() -> None:
    merged = auth.merge(_facts((), "offered"), _facts({"US"}, "refused"))
    assert merged == _facts({"US"}, "offered")
    assert auth.merge(None, _facts({"US"})) == _facts({"US"})


# --- stage 1 and the gate ------------------------------------------------------

RULES = {
    "profiles": {"global_remote": {"llm_gate_prompt": "prompts/remote_fit.md"}},
    "global": {"work_authorization": ["TR"], "sponsorship_required": True},
    "_managed_by_jobhunt_config": {"work_authorization": ["Turkey"], "sponsorship_required": True},
}


def _job(cfg, external_id="x1", **kwargs) -> int:
    fields = {
        "source": "ashby", "external_id": external_id, "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": f"Acme {external_id}",
        "remote_type": "remote", "country": "US",
        "description_text": "We build things. " * 40, "jd_completeness": "full",
    } | kwargs
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**fields))
        session.flush()
        return job.id


def _write_rules(cfg) -> None:
    path = cfg.home / deterministic.FILTERS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(RULES))


def _codes(cfg, job_id) -> list[str]:
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        return deterministic.evaluate(job, job.company, RULES).codes


def test_stage_one_drops_a_posting_that_says_so(cfg) -> None:
    job_id = _job(cfg, description_text="Must be authorized to work in the US. " + "Work. " * 80)
    assert "auth_required" in _codes(cfg, job_id)


def test_stage_one_keeps_a_posting_that_says_nothing(cfg) -> None:
    assert _codes(cfg, _job(cfg)) == []


def test_nothing_ticked_means_authorized_nowhere(cfg) -> None:
    """The rule as set: a hard requirement drops the job unless the matching
    place is ticked, and with nothing ticked no place is."""
    job_id = _job(cfg, description_text="Must be authorized to work in the US. " + "Work. " * 80)
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        codes = deterministic.evaluate(job, job.company, {"profiles": {}, "global": {}}).codes
        assert codes == ["auth_required"]


def test_the_gate_reading_is_stored_and_drops_the_job_at_once(cfg, tmp_path) -> None:
    _write_rules(cfg)
    job_id = _job(cfg)
    runner.run_deterministic(cfg)
    path = tmp_path / "v.json"
    path.write_text(json.dumps([{
        "job_id": job_id, "score": 0.9, "reasoning": "strong",
        "work_authorization_required": ["United States"], "visa_sponsorship": "unstated",
    }]))

    runner.ingest(cfg, path)

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.work_auth_required == ["US"] and job.auth_checked_at is not None
        score = session.scalars(select(Score).where(Score.job_id == job_id)).one()
        assert score.deterministic_pass is False
        assert "auth_required" in score.deterministic_notes["codes"]


def test_a_gate_verdict_without_the_fields_still_marks_the_job_read(cfg, tmp_path) -> None:
    _write_rules(cfg)
    job_id = _job(cfg)
    runner.run_deterministic(cfg)
    path = tmp_path / "v.json"
    path.write_text(json.dumps([{"job_id": job_id, "score": 0.9, "reasoning": "ok"}]))
    runner.ingest(cfg, path)
    with session_scope(cfg.db_path) as session:
        assert session.get(Job, job_id).auth_checked_at is not None


def test_a_high_scorer_read_before_this_existed_is_read_once_more(cfg, tmp_path) -> None:
    _write_rules(cfg)
    high, low = _job(cfg, "hi"), _job(cfg, "lo")
    runner.run_deterministic(cfg)
    with session_scope(cfg.db_path) as session:
        for job_id, value in ((high, 0.8), (low, 0.2)):
            session.scalars(select(Score).where(Score.job_id == job_id)).one().llm_score = value

    runner.emit(cfg, tmp_path / "b.json")

    payload = json.loads((tmp_path / "b.json").read_text())
    ids = [job["job_id"] for batch in payload["batches"] for job in batch["jobs"]]
    assert ids == [high]


def test_the_gate_prompt_states_the_ticks_and_asks_for_the_fields(cfg) -> None:
    text = runner._prompt_text(cfg, RULES, "global_remote", "CANDIDATE")
    assert "authorized to work in: Turkey" in text
    assert "need visa sponsorship" in text
    assert '"work_authorization_required"' in text and '"visa_sponsorship"' in text


def test_emit_hands_thin_jobs_to_the_fetcher_before_building_records(cfg, tmp_path) -> None:
    _write_rules(cfg)
    thin = _job(cfg, "thin", description_text="Short.", jd_completeness="snippet")
    _job(cfg, "full")
    runner.run_deterministic(cfg)
    seen: list[list[int]] = []

    def fetch(ids: list[int]) -> None:
        seen.append(ids)
        with session_scope(cfg.db_path) as session:
            session.get(Job, thin).description_text = "Fetched body. " * 60

    runner.emit(cfg, tmp_path / "b.json", fetch_missing=fetch)

    assert seen == [[thin]]
    payload = json.loads((tmp_path / "b.json").read_text())
    bodies = {job["job_id"]: job["description"] for b in payload["batches"] for job in b["jobs"]}
    assert bodies[thin].startswith("Fetched body.")


# --- fetching a posting's page -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://127.0.0.1/", "http://localhost:8765/api", "http://10.0.0.5/x",
     "http://169.254.169.254/latest/meta-data", "ftp://example.com/"],
)
def test_an_internal_or_odd_address_is_refused(url) -> None:
    with pytest.raises(jd_fetch.Unsafe):
        jd_fetch.check(url)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_a_page_fills_a_missing_description(cfg, monkeypatch) -> None:
    monkeypatch.setattr(jd_fetch, "check", lambda url: None)
    job_id = _job(cfg, source="personio", description_text=None, jd_completeness="none",
                  source_url="https://acme.jobs.personio.de/job/1")
    body = "<html><body><main><p>" + "Build APIs in Python. " * 40 + "</p></main></body></html>"

    filled = jd_fetch.fill(cfg, [job_id], client=_client(
        lambda request: httpx.Response(200, html=body)
    ))

    assert filled == 1
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.jd_completeness == "full" and job.jd_source == "page"
        assert "Build APIs in Python." in job.description_text


def test_a_redirect_to_an_internal_address_is_not_followed(cfg, monkeypatch) -> None:
    real = jd_fetch.check
    monkeypatch.setattr(
        jd_fetch, "check", lambda url: None if "example" in url else real(url)
    )
    job_id = _job(cfg, source="personio", description_text=None, jd_completeness="none",
                  source_url="https://example.com/j")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/cv/profile"})

    assert jd_fetch.fill(cfg, [job_id], client=_client(handler)) == 0
    assert calls == ["https://example.com/j"]


def test_linkedin_pages_are_never_fetched_here(cfg) -> None:
    job_id = _job(cfg, source="linkedin", description_text=None, jd_completeness="none",
                  source_url="https://www.linkedin.com/jobs/view/1")

    def handler(request):
        raise AssertionError("fetched a LinkedIn page")

    assert jd_fetch.fill(cfg, [job_id], client=_client(handler)) == 0
