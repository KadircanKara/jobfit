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

from jobhunt import preferences as prefs_module
from jobhunt.web.app import create_app


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
