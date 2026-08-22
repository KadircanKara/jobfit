"""Suggestion counts, which are the reason the vocabulary exists at all.

A count that is quietly wrong is worse than no count: it is read as permission
to drop a title. These pin the arithmetic so the query underneath can be
rewritten without anyone having to trust that it still adds up.
"""
from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select

from jobhunt.db import models
from jobhunt.db.session import session_scope
from jobhunt.web import vocab

_seeded = itertools.count()


def seed(cfg, rows: list[tuple[str, str, bool]]) -> None:
    """(title, country, is_active) triples, kept terse because the bodies do not matter.

    External ids come from a process-wide counter: (source, external_id) is
    unique, so a test that seeds twice must not restart the numbering.
    """
    with session_scope(cfg.db_path) as session:
        for title, country, active in rows:
            session.add(
                models.Job(
                    external_id=f"job-{next(_seeded)}",
                    source="greenhouse",
                    market="eu",
                    title=title,
                    title_normalized=title.lower(),
                    country=country,
                    is_active=active,
                )
            )


@pytest.fixture
def corpus(cfg):
    seed(
        cfg,
        [
            ("AI Engineer", "DE", True),
            ("Senior AI Engineer", "DE", True),
            ("ai engineer, platform", "NL", True),
            ("Machine Learning Engineer", "NL", True),
            ("Staff Data Scientist", "US", True),
            ("AI Engineer", "US", False),
            ("Chief Happiness Officer", "FR", True),
        ],
    )
    return cfg


def test_a_title_is_counted_wherever_it_appears_in_the_string(corpus):
    counts = vocab._title_counts(corpus)

    assert counts["AI Engineer"] == 3


def test_the_match_ignores_case(corpus):
    counts = vocab._title_counts(corpus)

    assert counts["Machine Learning Engineer"] == 1
    assert counts["Data Scientist"] == 1


def test_inactive_jobs_are_not_counted(corpus):
    counts = vocab._title_counts(corpus)

    assert counts["AI Engineer"] == 3, "the inactive fourth must not be in there"


def test_a_suggestion_nobody_is_hiring_for_comes_back_as_zero(corpus):
    counts = vocab._title_counts(corpus)

    assert counts["Prompt Engineer"] == 0


def test_every_suggestion_gets_a_key(corpus):
    counts = vocab._title_counts(corpus)

    assert set(counts) == set(vocab.TITLE_SUGGESTIONS)


def test_an_empty_corpus_still_answers_for_every_suggestion(cfg):
    counts = vocab._title_counts(cfg)

    assert set(counts) == set(vocab.TITLE_SUGGESTIONS)
    assert set(counts.values()) == {0}


def test_the_built_payload_sorts_titles_by_count(corpus):
    titles = vocab.build(corpus)["titles"]

    assert [row["count"] for row in titles] == sorted(
        (row["count"] for row in titles), reverse=True
    )
    assert titles[0]["value"] == "AI Engineer"


# --- caching ----------------------------------------------------------------
#
# The counts are worth about a third of a second on a real corpus, and the form
# asks for them on every visit. Cached against a corpus generation rather than a
# clock, so a sync is reflected on the next request and not one TTL later.


def test_a_second_call_on_an_unchanged_corpus_does_not_touch_the_database(corpus, monkeypatch):
    vocab.build(corpus)

    def explode(_config):
        raise AssertionError("the counts were recomputed for an unchanged corpus")

    monkeypatch.setattr(vocab, "_title_counts", explode)
    monkeypatch.setattr(vocab, "_country_counts", explode)

    assert vocab.build(corpus)["titles"], "the cached payload still has to come back"


def test_the_cached_payload_equals_the_freshly_computed_one(corpus):
    fresh = vocab.build(corpus)
    cached = vocab.build(corpus)

    assert cached == fresh


def test_a_new_job_is_reflected_on_the_next_call(corpus):
    before = {row["value"]: row["count"] for row in vocab.build(corpus)["titles"]}
    seed(corpus, [("Prompt Engineer", "DE", True)])

    after = {row["value"]: row["count"] for row in vocab.build(corpus)["titles"]}

    assert before["Prompt Engineer"] == 0
    assert after["Prompt Engineer"] == 1


def test_deactivating_a_job_is_reflected_on_the_next_call(corpus):
    before = {row["value"]: row["count"] for row in vocab.build(corpus)["titles"]}
    with session_scope(corpus.db_path) as session:
        job = session.scalars(
            select(models.Job).where(models.Job.title == "Machine Learning Engineer")
        ).one()
        job.is_active = False

    after = {row["value"]: row["count"] for row in vocab.build(corpus)["titles"]}

    assert before["Machine Learning Engineer"] == 1
    assert after["Machine Learning Engineer"] == 0, (
        "a deactivation leaves max(id) alone, so the count alone has to catch it"
    )


def test_two_corpora_do_not_share_a_cache_entry(corpus, tmp_path_factory, cfg_for):
    other = cfg_for(tmp_path_factory.mktemp("other"))
    seed(other, [("Prompt Engineer", "FR", True)])

    mine = {row["value"]: row["count"] for row in vocab.build(corpus)["titles"]}
    theirs = {row["value"]: row["count"] for row in vocab.build(other)["titles"]}

    assert mine["AI Engineer"] == 3
    assert theirs["AI Engineer"] == 0
    assert theirs["Prompt Engineer"] == 1


def test_a_caller_that_edits_the_payload_cannot_corrupt_the_cache(corpus):
    first = vocab.build(corpus)
    first["titles"][0]["count"] = 9999
    first["titles"].append({"value": "Astronaut", "label": "", "count": 5})
    first["active_jobs"] = -1
    del first["locations"]

    second = vocab.build(corpus)

    assert second["active_jobs"] == 6
    assert "locations" in second
    assert "Astronaut" not in [row["value"] for row in second["titles"]]
    assert second["titles"][0]["count"] == 3, "the nested row must be a copy too"
