"""The category panel's HTTP surface.

Approving a feed is a spending decision the user makes in a browser, so the
route has to report what happened to earlier approvals too: a board that died
on a bad slug must be visible, not silently absent.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobhunt import preferences as prefs_module
from jobhunt.db import models
from jobhunt.db.session import session_scope
from jobhunt.discovery import categories
from jobhunt.web.app import create_app

FULL_STACK = {"provider": "wwr", "token": "remote-full-stack-programming-jobs"}


def kill_board(cfg, token: str) -> None:
    """Do to a board what `record_fetch_failures` (sync.py:274) does to a
    candidate whose slug 301s away: kill it, without touching its note."""
    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == token)
        ).one()
        board.status = "dead"
        board.consecutive_errors = 1


@pytest.fixture
def client(cfg):
    return TestClient(create_app(config=cfg))


def seed_payload(cfg) -> None:
    items = "".join(
        f"<item><title>Co: Python Developer</title><link>https://x.test/{i}</link>"
        "<category>Full-Stack Programming</category></item>"
        for i in range(3)
    )
    folder = pathlib.Path(cfg.raw_dir) / "wwr" / "20260820T120000"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "wwr__all.json").write_text(
        json.dumps({
            "run_key": "20260820T120000", "source": "wwr", "provider": "wwr",
            "token": "all", "market": "global_remote", "board_id": 1,
            "fetched_at": "2026-08-20T12:00:00",
            "payload": f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>',
        }),
        encoding="utf-8",
    )
    prefs_module.save(cfg, prefs_module.Preferences(titles=["Python Developer"]))


def test_the_panel_is_empty_before_any_sync(client):
    body = client.get("/api/feeds").json()

    assert body["proposals"] == []
    assert body["approved"] == []


def test_a_category_with_evidence_is_proposed(cfg, client):
    seed_payload(cfg)

    proposals = client.get("/api/feeds").json()["proposals"]

    top = proposals[0]
    assert top["category"] == "Full-Stack Programming"
    assert top["matched"] == 3
    assert top["token"] == "remote-full-stack-programming-jobs"


def test_approving_moves_a_proposal_into_the_approved_list(cfg, client):
    seed_payload(cfg)

    body = client.post(
        "/api/feeds",
        json={"approve": [{"provider": "wwr", "token": "remote-full-stack-programming-jobs"}]},
    ).json()

    approved = body["approved"]
    assert [row["token"] for row in approved] == ["remote-full-stack-programming-jobs"]
    assert approved[0]["status"] == "candidate"


def test_an_approved_feed_reports_its_board_status_back(cfg, client):
    seed_payload(cfg)
    client.post(
        "/api/feeds",
        json={"approve": [{"provider": "wwr", "token": "remote-full-stack-programming-jobs"}]},
    )

    row = client.get("/api/feeds").json()["approved"][0]

    assert set(row) >= {"provider", "token", "status", "last_job_count", "last_fetched_at"}


def test_retiring_removes_it_from_the_approved_list(cfg, client):
    seed_payload(cfg)
    payload = {"provider": "wwr", "token": "remote-full-stack-programming-jobs"}
    client.post("/api/feeds", json={"approve": [payload]})

    body = client.post("/api/feeds", json={"retire": [payload]}).json()

    assert body["approved"] == []


def test_an_unknown_provider_is_refused(client):
    response = client.post(
        "/api/feeds", json={"approve": [{"provider": "nonesuch", "token": "x"}]}
    )

    assert response.status_code == 422
    assert "nonesuch" in response.json()["message"]


def test_an_unknown_provider_blocks_the_whole_batch(cfg, client):
    """One bad provider in a batch must not let the good pairs through either.

    Otherwise a typo'd row silently drops while its siblings get approved, and
    the 422 the browser sees would be lying about what happened server-side.
    """
    seed_payload(cfg)
    good = {"provider": "wwr", "token": "remote-full-stack-programming-jobs"}

    response = client.post(
        "/api/feeds", json={"approve": [good, {"provider": "nonesuch", "token": "x"}]}
    )

    assert response.status_code == 422
    approved = client.get("/api/feeds").json()["approved"]
    assert approved == []


def test_a_non_list_approve_value_is_refused_not_crashed(client):
    response = client.post("/api/feeds", json={"approve": "wwr"})

    assert response.status_code == 422


def test_a_non_object_row_is_refused_not_crashed(client):
    response = client.post("/api/feeds", json={"approve": ["nope"]})

    assert response.status_code == 422


def test_a_missing_provider_gets_a_sensible_message(client):
    response = client.post("/api/feeds", json={"approve": [{"token": "x"}]})

    assert response.status_code == 422
    assert "None" not in response.json()["message"]


def test_an_approved_feed_that_died_still_reports_back(cfg, client):
    """The whole justification for shipping no prober is that the panel says
    what became of each approved guess. Filtering dead rows out of the
    approved list would make a failed feed invisible AND put it back at the
    top of the proposals looking untried, so the user re-approves it forever.
    """
    seed_payload(cfg)
    client.post("/api/feeds", json={"approve": [FULL_STACK]})
    kill_board(cfg, FULL_STACK["token"])

    body = client.get("/api/feeds").json()

    assert [row["token"] for row in body["approved"]] == [FULL_STACK["token"]]
    assert body["approved"][0]["status"] == "dead", "Feeds.tsx renders 'did not resolve'"
    assert body["proposals"] == [], "already tried; its outcome is on screen"


def test_a_retired_feed_leaves_the_panel_and_returns_as_a_proposal(cfg, client):
    seed_payload(cfg)
    client.post("/api/feeds", json={"approve": [FULL_STACK]})

    body = client.post("/api/feeds", json={"retire": [FULL_STACK]}).json()

    assert body["approved"] == []
    assert [row["category"] for row in body["proposals"]] == ["Full-Stack Programming"]


def test_a_retired_feed_can_be_approved_again(cfg, client):
    seed_payload(cfg)
    client.post("/api/feeds", json={"approve": [FULL_STACK]})
    client.post("/api/feeds", json={"retire": [FULL_STACK]})

    body = client.post("/api/feeds", json={"approve": [FULL_STACK]}).json()

    assert [row["token"] for row in body["approved"]] == [FULL_STACK["token"]]
    assert body["approved"][0]["status"] == "candidate"
    assert body["proposals"] == []


def test_a_dead_approved_feed_can_be_dismissed_by_retiring_it(cfg, client):
    seed_payload(cfg)
    client.post("/api/feeds", json={"approve": [FULL_STACK]})
    kill_board(cfg, FULL_STACK["token"])

    body = client.post("/api/feeds", json={"retire": [FULL_STACK]}).json()

    assert body["approved"] == []
    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == FULL_STACK["token"])
        ).one()
        assert board.notes == categories.RETIRED_NOTE, "PLAN.md section 6: never deleted"


@pytest.mark.parametrize("token", [{"a": 1}, ["x"], 5.5, True])
def test_a_non_string_token_is_refused_not_crashed(client, token):
    """A dict or list bound straight into the SQL parameter used to raise
    `sqlite3.ProgrammingError` as a 500, contradicting `_BadFeedRequest`'s
    promise that every malformed shape becomes a clean 422."""
    response = client.post(
        "/api/feeds", json={"approve": [{"provider": "wwr", "token": token}]}
    )

    assert response.status_code == 422


def test_a_non_string_provider_is_refused_not_crashed(client):
    """`{"a": 1} in VOCABULARY` raises TypeError on the unhashable dict."""
    response = client.post(
        "/api/feeds", json={"approve": [{"provider": {"a": 1}, "token": "x"}]}
    )

    assert response.status_code == 422


def test_the_panel_says_which_empty_state_it_is_in(cfg, client):
    """"Run a sync first" is wrong advice when the real cause is no titles:
    `propose` returns [] for an empty title list whatever is on disk."""
    assert client.get("/api/feeds").json()["has_titles"] is False

    seed_payload(cfg)

    assert client.get("/api/feeds").json()["has_titles"] is True
