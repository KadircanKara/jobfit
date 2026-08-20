"""The shortlist and the digest. No prompts, no network.

The digest is what a cron line pipes into a file, so it has to be plain text,
make no LLM call, and exit cleanly on an empty corpus.
"""
from __future__ import annotations

import datetime as dt

from jobhunt import applications, store
from jobhunt.db.models import Job, Score, utcnow
from jobhunt.db.session import session_scope
from jobhunt.render import review
from jobhunt.sources.base import JobPosting

FILTERS_THRESHOLD = 0.7


def make_scored(cfg, score_value=0.8, **kwargs) -> int:
    defaults = {
        "source": "ashby", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "description_text": "We build things. " * 40, "remote_type": "remote",
        "jd_completeness": "full", "apply_url": "https://jobs.ashbyhq.com/acme/1",
    }
    reasoning = kwargs.pop("reasoning", "Strong match on FastAPI and async work")
    boost = kwargs.pop("boost", 1.0)
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        session.add(Score(
            job_id=job.id, profile=job.market, deterministic_pass=True,
            deterministic_notes={"passed": True, "boost": boost},
            llm_score=score_value, llm_reasoning=reasoning,
        ))
        return job.id


def test_shortlist_returns_gated_survivors(cfg) -> None:
    make_scored(cfg)
    cards = review.shortlist(cfg)
    assert len(cards) == 1
    assert cards[0].title == "Senior Backend Engineer"
    assert cards[0].score == 0.8


def test_a_job_below_the_market_threshold_does_not_surface(cfg) -> None:
    make_scored(cfg, score_value=0.4)
    assert review.shortlist(cfg) == []


def test_ungated_jobs_are_hidden_unless_asked_for(cfg) -> None:
    make_scored(cfg, score_value=None)
    assert review.shortlist(cfg) == []
    assert len(review.shortlist(cfg, include_unscored=True)) == 1


def test_ordering_is_gate_score_times_deterministic_boost(cfg) -> None:
    low = make_scored(cfg, score_value=0.75, external_id="a")
    high = make_scored(cfg, score_value=0.72, external_id="b", boost=1.4,
                       title="Founding Backend Engineer")
    cards = review.shortlist(cfg)
    assert [card.job_id for card in cards] == [high, low]


def test_an_applied_job_never_surfaces_again(cfg, tmp_path) -> None:
    cfg.raw["tailoring"]["applications_root"] = str(tmp_path / "apps")
    job_id = make_scored(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).jd_quality_score = 0.9
        applications.apply(cfg, session, job_id)
    assert review.shortlist(cfg) == []


def test_a_skipped_job_never_surfaces_again(cfg) -> None:
    job_id = make_scored(cfg)
    with session_scope(cfg.db_path) as session:
        applications.skip(session, job_id, "stack mismatch")
    assert review.shortlist(cfg) == []


def test_skipping_one_source_row_hides_the_whole_cluster(cfg) -> None:
    first = make_scored(cfg)
    second = make_scored(cfg, source="lever", external_id="y1")
    with session_scope(cfg.db_path) as session:
        session.get(Job, first).canonical_job_id = first
        session.get(Job, second).canonical_job_id = first
        applications.skip(session, second, "not interested")
    assert review.shortlist(cfg) == []


def test_since_filters_by_first_seen(cfg) -> None:
    job_id = make_scored(cfg)
    with session_scope(cfg.db_path) as session:
        session.get(Job, job_id).first_seen_at = utcnow() - dt.timedelta(days=10)
    assert review.shortlist(cfg, since_days=1) == []
    assert len(review.shortlist(cfg, since_days=30)) == 1


def test_limit_is_respected(cfg) -> None:
    for index in range(5):
        make_scored(cfg, external_id=f"x{index}")
    assert len(review.shortlist(cfg, limit=3)) == 3


def test_card_rendering_carries_the_reasoning_and_the_link(cfg) -> None:
    make_scored(cfg)
    text = review.render_card(review.shortlist(cfg)[0])
    assert "Senior Backend Engineer" in text
    assert "score 0.80" in text
    assert "Strong match" in text
    assert "https://jobs.ashbyhq.com/acme/1" in text


def test_a_snippet_jd_is_flagged_on_the_card(cfg) -> None:
    make_scored(cfg, jd_completeness="snippet")
    assert "jd: snippet" in review.render_card(review.shortlist(cfg)[0])


def test_also_on_lists_the_other_sources(cfg) -> None:
    first = make_scored(cfg)
    second = make_scored(cfg, source="lever", external_id="y1")
    with session_scope(cfg.db_path) as session:
        session.get(Job, first).canonical_job_id = first
        session.get(Job, second).canonical_job_id = first
    card = next(c for c in review.shortlist(cfg) if c.job_id == first)
    assert card.also_on == ["lever"]


def test_digest_is_plain_text_and_survives_an_empty_corpus(cfg) -> None:
    assert "nothing new" in review.render_digest([])
    make_scored(cfg)
    digest = review.render_digest(review.shortlist(cfg))
    assert digest.endswith("\n")
    assert "[" in digest and "score 0.80" in digest
    assert "\x1b[" not in digest  # no colour codes: this gets piped into a file
