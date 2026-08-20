"""Per-board progress, so a caller can show a scrape as it happens.

The web app draws a bar per source that fills board by board. Without a hook
inside the fetch loop, the only signal available is "the source finished",
which for a fifty-board source is a bar that sits still for a minute and then
jumps to full.
"""
from __future__ import annotations

import pytest

from jobhunt import sync


class StubAdapter:
    """Fetches nothing, sleeps for nothing, fails on the token told to fail."""

    def __init__(self, fail_token=None):
        self.fail_token = fail_token
        self.rate_limit = type("NoWait", (), {"sleep": lambda self: None})()

    def fetch(self, ref, client):
        if ref.token == self.fail_token:
            raise ConnectionError("unreachable")
        return {"jobs": []}


@pytest.fixture
def refs():
    return [
        sync.BoardRef(provider="greenhouse", token=f"co{index}", market="global_remote", extra={})
        for index in range(3)
    ]


def test_progress_is_reported_once_per_board(cfg, refs, monkeypatch):
    monkeypatch.setattr(sync.source_registry, "get", lambda name: lambda: StubAdapter())
    seen = []

    sync.fetch_pass(
        cfg, "greenhouse", refs, "run1",
        progress=lambda done, total, token: seen.append((done, total, token)),
    )

    assert seen == [(1, 3, "co0"), (2, 3, "co1"), (3, 3, "co2")]


def test_a_failed_board_still_advances_the_count(cfg, refs, monkeypatch):
    """The bar tracks boards attempted. A dead board must not stall it."""
    monkeypatch.setattr(
        sync.source_registry, "get", lambda name: lambda: StubAdapter(fail_token="co1")
    )
    seen = []

    sync.fetch_pass(
        cfg, "greenhouse", refs, "run1",
        progress=lambda done, total, token: seen.append(done),
    )

    assert seen == [1, 2, 3]


def test_a_caller_that_asks_to_stop_gets_no_further_boards(cfg, refs, monkeypatch):
    monkeypatch.setattr(sync.source_registry, "get", lambda name: lambda: StubAdapter())
    seen = []

    fetched, _, _ = sync.fetch_pass(
        cfg, "greenhouse", refs, "run1",
        progress=lambda done, total, token: seen.append(done),
        should_stop=lambda: len(seen) >= 2,
    )

    assert seen == [1, 2]
    assert fetched == 2


def test_without_a_hook_nothing_changes(cfg, refs, monkeypatch):
    monkeypatch.setattr(sync.source_registry, "get", lambda name: lambda: StubAdapter())

    fetched, failed, _ = sync.fetch_pass(cfg, "greenhouse", refs, "run1")

    assert (fetched, failed) == (3, [])
