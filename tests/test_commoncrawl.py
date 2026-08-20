"""Strategy C parse and store passes, against a stored CDX page. No network.

fetch and parse are separate here for the same reason they are separate in sync:
a CDX page is a megabyte of JSON lines and the index server asks not to be
hammered, so re-parsing must never re-query.
"""
from __future__ import annotations

import json

import pytest

from jobhunt.db.models import Board
from jobhunt.db.session import session_scope
from jobhunt.discovery import commoncrawl as cc

URLS = [
    "https://boards.greenhouse.io/stripe/jobs/1234",
    "https://boards.greenhouse.io/stripe/jobs/5678",       # same token twice
    "https://boards.greenhouse.io/anthropic/jobs/9",
    "https://boards.greenhouse.io/robots.txt",             # junk
    "https://boards.greenhouse.io/YOUR_COMPANY/jobs/1",    # placeholder
    "https://jobs.ashbyhq.com/ramp/abc",                   # a different provider
]


def place_page(cfg, provider: str = "greenhouse", run_key: str = "R1", urls=None):
    path = cfg.raw_dir / "commoncrawl" / run_key / f"{provider}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps({"url": u}) for u in (urls if urls is not None else URLS)),
        encoding="utf-8",
    )
    return path


def test_parse_collapses_duplicates_and_drops_junk(cfg) -> None:
    records, hits = cc.parse_file(place_page(cfg))
    assert records == len(URLS)
    tokens = {(h.provider, h.token) for h in hits}
    assert tokens == {("greenhouse", "stripe"), ("greenhouse", "anthropic"), ("ashby", "ramp")}


def test_run_from_raw_stores_only_the_requested_provider(cfg) -> None:
    """A greenhouse page can contain an ashby URL. It belongs to the ashby
    backfill, not this one, or the counts stop meaning anything."""
    place_page(cfg)
    result = cc.run(cfg, providers=("greenhouse",), from_raw="R1")

    assert result.new_boards == 2
    assert result.by_provider == {"greenhouse": 2}
    with session_scope(cfg.db_path) as session:
        boards = session.query(Board).all()
        assert {b.token for b in boards} == {"stripe", "anthropic"}
        assert all(b.provider == "greenhouse" for b in boards)
        assert all(b.status == "candidate" for b in boards)
        assert all(b.discovered_via == "commoncrawl" for b in boards)
        # NULL next_fetch_at is what makes the next sync validate them.
        assert all(b.next_fetch_at is None for b in boards)


def test_backfill_is_idempotent(cfg) -> None:
    place_page(cfg)
    cc.run(cfg, providers=("greenhouse",), from_raw="R1")
    again = cc.run(cfg, providers=("greenhouse",), from_raw="R1")
    assert again.new_boards == 0
    assert again.known_boards == 2


def test_dry_run_writes_nothing(cfg) -> None:
    place_page(cfg)
    result = cc.run(cfg, providers=("greenhouse",), from_raw="R1", dry_run=True)
    assert result.new_boards == 2
    with session_scope(cfg.db_path) as session:
        assert session.query(Board).count() == 0


def test_a_missing_provider_file_is_recorded_not_raised(cfg) -> None:
    place_page(cfg)
    result = cc.run(cfg, providers=("greenhouse", "ashby"), from_raw="R1")
    assert result.failures == ["ashby:missing"]
    assert result.new_boards == 2


def test_an_unknown_provider_is_a_loud_error(cfg) -> None:
    with pytest.raises(KeyError):
        cc.run(cfg, providers=("monster",), from_raw="R1")


def test_backfill_boards_carry_the_requested_market(cfg) -> None:
    place_page(cfg)
    cc.run(cfg, providers=("greenhouse",), from_raw="R1", market="tr_local")
    with session_scope(cfg.db_path) as session:
        assert {b.market for b in session.query(Board).all()} == {"tr_local"}


def test_a_malformed_line_does_not_stop_the_parse(cfg) -> None:
    path = place_page(cfg)
    path.write_text(
        'not json\n{"url": "https://boards.greenhouse.io/stripe/jobs/1"}\n', encoding="utf-8"
    )
    records, hits = cc.parse_file(path)
    assert records == 2
    assert {h.token for h in hits} == {"stripe"}
