"""Which boards a run fetches, and the on-demand check of the rest. No network."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from jobhunt import board_scope, store, sync
from jobhunt.db.models import Board, Job, Score, utcnow
from jobhunt.db.session import session_scope
from jobhunt.render import review
from jobhunt.sources.base import JobPosting


def board(cfg, token: str, status: str = "validated", via: str = "yc", provider: str = "ashby") -> int:
    with session_scope(cfg.db_path) as session:
        row = store.get_or_create_board(session, provider, token, via, "global_remote")
        row.status = status
        session.flush()
        return row.id


def post(cfg, board_id: int, title: str, external_id: str, **fields) -> int:
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(
            source="ashby", external_id=external_id, market="global_remote", title=title,
            company_name="Acme", description_text="We build things. " * 40,
            remote_type="remote", jd_completeness="full",
            apply_url=f"https://jobs.ashbyhq.com/acme/{external_id}",
        ))
        job.board_id = board_id
        for key, value in fields.items():
            setattr(job, key, value)
        session.flush()
        return job.id


def tokens(refs) -> set[str]:
    return {ref.token for ref in refs}


@pytest.fixture
def corpus(cfg):
    """One board of each kind a run has to tell apart."""
    ids = {
        "match": board(cfg, "match"),
        "nomatch": board(cfg, "nomatch"),
        "excluded": board(cfg, "excluded"),
        "empty": board(cfg, "empty", status="empty"),
        "candidate": board(cfg, "candidate", status="candidate"),
        "dead": board(cfg, "dead", status="dead"),
        "feed": board(cfg, "feed", via="feed"),
    }
    post(cfg, ids["match"], "Senior Backend Engineer", "m1")
    post(cfg, ids["nomatch"], "Bakery Shift Lead", "n1")
    post(cfg, ids["excluded"], "Sales Engineer", "x1")
    return ids


# --- relevance ---------------------------------------------------------------------


def test_a_board_is_relevant_once_it_has_posted_a_matching_title(cfg, corpus) -> None:
    assert board_scope.relevant_board_ids(cfg) == {corpus["match"]}


def test_a_run_fetches_relevant_boards_and_feeds_only(cfg, corpus) -> None:
    assert tokens(board_scope.relevant_boards(cfg, "ashby")) == {"feed", "match"}


def test_a_hand_picked_board_is_fetched_every_run_like_a_feed(cfg, corpus) -> None:
    """Picked by name, so never left waiting for a matching title to appear."""
    board(cfg, "picked", status="candidate", via="manual")
    assert tokens(board_scope.relevant_boards(cfg, "ashby")) == {"feed", "match", "picked"}
    assert "picked" not in tokens(board_scope.other_boards(cfg, "ashby", include_dead=True))


def test_the_check_fetches_everything_a_run_skips(cfg, corpus) -> None:
    assert tokens(board_scope.other_boards(cfg, "ashby", include_dead=False)) == {
        "nomatch", "excluded", "empty", "candidate",
    }
    assert "dead" in tokens(board_scope.other_boards(cfg, "ashby", include_dead=True))


def test_a_normal_sync_fetches_no_candidate_and_no_irrelevant_board(cfg, corpus, monkeypatch) -> None:
    seen: list[str] = []

    def fetch(config, source, refs, run_key, **hooks):
        seen.extend(ref.token for ref in refs)
        return len(refs), [], []

    monkeypatch.setattr(sync, "fetch_pass", fetch)
    sync.sync_source(cfg, "ashby")

    assert sorted(seen) == ["feed", "match"]


def test_changing_the_titles_changes_the_boards_at_once(cfg, corpus, monkeypatch) -> None:
    monkeypatch.setattr(
        board_scope.deterministic, "load_filters",
        lambda config: {"global": {"require_titles_regex": ["(?i)bakery"]}},
    )
    assert board_scope.relevant_board_ids(cfg) == {corpus["nomatch"]}


# --- the check -----------------------------------------------------------------------


def test_the_check_fetches_the_rest_and_promotes_what_now_matches(cfg, corpus) -> None:
    fetched: dict[str, list[str]] = {}

    def fetch(config, source, refs, run_key, **hooks):
        fetched[source] = [ref.token for ref in refs]
        return len(refs), [], []

    def finish(config, source, prefetched):
        # The candidate turns out to be hiring engineers.
        post(config, corpus["candidate"], "Machine Learning Engineer", "c1")

    state = board_scope.run(cfg, ["ashby"], board_scope.CheckState(), fetch=fetch, finish=finish)

    assert set(fetched["ashby"]) == {"nomatch", "excluded", "empty", "candidate", "dead"}
    assert state.became_relevant == 1
    assert state.sources["ashby"].new_relevant == 1
    assert corpus["candidate"] in board_scope.relevant_board_ids(cfg)


def test_dead_boards_are_retried_once_only(cfg, corpus) -> None:
    rounds: list[set[str]] = []

    def fetch(config, source, refs, run_key, **hooks):
        rounds.append({ref.token for ref in refs})
        return len(refs), [], []

    for _ in range(2):
        board_scope.run(
            cfg, ["ashby"], board_scope.CheckState(), fetch=fetch, finish=lambda *a, **k: None
        )

    assert "dead" in rounds[0] and "dead" not in rounds[1]
    assert board_scope.summary(cfg, ["ashby"])["includes_dead"] is False


def test_the_summary_counts_both_sides(cfg, corpus) -> None:
    counts = board_scope.summary(cfg, ["ashby"])
    assert (counts["relevant"], counts["other"]) == (2, 5)
    assert counts["last_checked_at"] is None


# --- the web ----------------------------------------------------------------------------


@pytest.fixture
def client(cfg):
    from jobhunt.web.app import create_app

    return TestClient(create_app(config=cfg))


def test_the_check_and_a_run_never_overlap(client, monkeypatch) -> None:
    jh = client.app.state.jh
    monkeypatch.setattr(jh.boards.state, "running", True)
    assert client.post("/api/runs").status_code == 409

    monkeypatch.setattr(jh.boards.state, "running", False)
    fake_run = type("S", (), {"state": type("R", (), {"running": True, "finished_at": None})()})()
    monkeypatch.setattr(jh, "supervisor", fake_run)
    assert client.post("/api/boards/check").status_code == 409


def test_the_board_counts_come_back(client) -> None:
    body = client.get("/api/boards").json()
    assert {"relevant", "other", "includes_dead", "last_checked_at", "check"} <= set(body)
    assert body["check"]["running"] is False


# --- the shortlist cutoff -----------------------------------------------------------------


def scored(cfg, job_id: int) -> None:
    with session_scope(cfg.db_path) as session:
        session.add(Score(
            job_id=job_id, profile="global_remote", deterministic_pass=True,
            deterministic_notes={"passed": True, "boost": 1.0}, llm_score=0.9,
        ))


def test_a_board_job_is_shown_however_long_since_its_board_was_fetched(cfg, corpus) -> None:
    job_id = post(cfg, corpus["match"], "Staff Backend Engineer", "old",
                  last_seen_at=utcnow() - dt.timedelta(days=40))
    scored(cfg, job_id)
    assert job_id in {card.job_id for card in review.shortlist(cfg)}


def test_a_board_job_missing_from_its_boards_latest_fetch_is_not(cfg, corpus) -> None:
    job_id = post(cfg, corpus["match"], "Staff Backend Engineer", "gone", missed_runs=1)
    scored(cfg, job_id)
    assert job_id not in {card.job_id for card in review.shortlist(cfg)}


def test_nothing_here_touches_other_platforms(cfg, corpus) -> None:
    board(cfg, "gh", provider="greenhouse")
    with session_scope(cfg.db_path) as session:
        assert session.query(Board).filter_by(provider="greenhouse").count() == 1
    assert board_scope.relevant_boards(cfg, "greenhouse") == []
    with session_scope(cfg.db_path) as session:
        assert session.query(Job).count() == 3


# --- fetching every source at once ---------------------------------------------------------


def test_every_source_is_fetched_at_once_then_stored_from_what_was_fetched(cfg, corpus, monkeypatch) -> None:
    import threading

    board(cfg, "gh", provider="greenhouse")
    post(cfg, board(cfg, "ghmatch", provider="greenhouse"), "Backend Engineer", "g1")
    both_in = threading.Barrier(2, timeout=5)

    def fetch(config, source, refs, run_key, **hooks):
        both_in.wait()  # only returns once the other source is fetching too
        return len(refs), [], []

    monkeypatch.setattr(sync, "fetch_pass", fetch)
    done = {source: pre for source, pre, error in sync.prefetch_as_done(cfg, ["ashby", "greenhouse"])}

    assert tokens(done["ashby"].refs) == {"feed", "match"}
    assert tokens(done["greenhouse"].refs) == {"ghmatch"}

    monkeypatch.setattr(sync, "fetch_pass", lambda *a, **k: pytest.fail("stored, not fetched again"))
    result = sync.sync_source(cfg, "ashby", prefetched=done["ashby"])
    assert (result.boards, result.raw_fetched) == (2, 2)


def test_a_fast_source_comes_back_while_a_slow_one_is_still_fetching(cfg, corpus, monkeypatch) -> None:
    import threading

    board(cfg, "gh", provider="greenhouse")
    post(cfg, board(cfg, "ghmatch", provider="greenhouse"), "Backend Engineer", "g1")
    release = threading.Event()
    stopped: list[bool] = []

    def fetch(config, source, refs, run_key, should_stop=None, **hooks):
        if source == "greenhouse":  # the slow one: holds until let go, or told to stop
            while not release.wait(0.01):
                if should_stop():
                    stopped.append(True)
                    break
        return len(refs), [], []

    monkeypatch.setattr(sync, "fetch_pass", fetch)
    order = sync.prefetch_as_done(cfg, ["ashby", "greenhouse"])
    source, prefetched, error = next(order)

    assert (source, error) == ("ashby", None) and not release.is_set()
    order.close()  # a stop: the slow fetch is told to end rather than waited out
    assert stopped == [True]


def test_a_source_whose_fetch_raises_is_reported_not_fatal(cfg, corpus, monkeypatch) -> None:
    def fetch(config, source, refs, run_key, **hooks):
        raise RuntimeError("boom")

    monkeypatch.setattr(sync, "fetch_pass", fetch)
    assert list(sync.prefetch_as_done(cfg, ["ashby"])) == [("ashby", None, "RuntimeError: boom")]
