"""Category proposals, scored against payloads already on disk.

The panel these feed is a spending decision: every approved category is a
request a day, forever. So the numbers behind it are counted from real stored
postings rather than guessed from the words in a category name.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from conftest import FIXTURES

from jobhunt.discovery import categories


def write_run(config, provider: str, run_key: str, token: str, payload: str) -> None:
    """Reproduce one raw payload envelope exactly as sync.py:218 writes it."""
    folder = pathlib.Path(config.raw_dir) / provider / run_key
    folder.mkdir(parents=True, exist_ok=True)
    envelope = {
        "run_key": run_key,
        "source": provider,
        "provider": provider,
        "token": token,
        "market": "global_remote",
        "board_id": 1,
        "fetched_at": f"2026-08-{run_key[6:8]}T12:00:00",
        "payload": payload,
    }
    (folder / f"{provider}__{token}.json").write_text(
        json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def wwr_corpus(cfg):
    payload = (FIXTURES / "wwr_programming.xml").read_text(encoding="utf-8")
    write_run(cfg, "wwr", "20260820T120000", "all", payload)
    return cfg


def test_observe_reads_the_categories_the_provider_states(wwr_corpus):
    postings = categories.observe(wwr_corpus, "wwr")

    labels = {label for posting in postings for label in posting.labels}
    assert labels, "the wwr fixture carries <category> on every item"
    assert all(isinstance(p.title, str) and p.title for p in postings)


def test_observe_returns_nothing_for_a_provider_with_no_stored_runs(cfg):
    assert categories.observe(cfg, "wwr") == []


def test_arbeitnow_is_not_in_the_vocabulary(cfg):
    assert "arbeitnow" not in categories.VOCABULARY, (
        "arbeitnow has no narrowing parameter, so it cannot be proposed"
    )
