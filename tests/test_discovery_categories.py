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


def test_observe_skips_a_non_dict_envelope_but_keeps_the_good_sibling(wwr_corpus):
    """A bare JSON list is valid JSON but not the dict envelope sync.py writes.

    One stray or corrupted file in a run directory must not take out the
    whole provider: `observe` should skip it silently and still return the
    postings read from the well-formed sibling file in the same directory.
    """
    folder = pathlib.Path(wwr_corpus.raw_dir) / "wwr" / "20260820T120000"
    (folder / "wwr__broken.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )

    postings = categories.observe(wwr_corpus, "wwr")

    labels = {label for posting in postings for label in posting.labels}
    assert labels, "the good sibling file's postings must still come back"


def test_a_posting_seen_in_two_runs_is_counted_once(cfg):
    payload = (FIXTURES / "wwr_programming.xml").read_text(encoding="utf-8")
    write_run(cfg, "wwr", "20260820T120000", "all", payload)
    write_run(cfg, "wwr", "20260821T120000", "all", payload)

    once = categories.observe(cfg, "wwr")

    ids = [p.posting_id for p in once]
    assert len(ids) == len(set(ids)), "the same link on two days is one posting"


def _wwr_payload_for(link: str) -> str:
    """A single-item wwr feed whose posting id is distinct per call.

    `test_only_the_newest_runs_are_read` needs each run directory to carry a
    posting the others do not, otherwise `observe`'s own dedup-by-id makes any
    `runs` value produce the same set and the test cannot tell a real window
    slice from a deleted one.
    """
    return (
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>Example: Engineer</title>"
        f"<link>{link}</link><category>Full-Stack Programming</category>"
        "</item></channel></rss>"
    )


def test_only_the_newest_runs_are_read(cfg):
    for day in range(10, 20):
        run_key = f"202608{day}T120000"
        write_run(
            cfg, "wwr", run_key, "all",
            _wwr_payload_for(f"https://example.test/{run_key}"),
        )

    # 10 run directories exist (day 10..19, sorted newest first: 19..10).
    # A window of 2 must read only the newest two directories' postings.
    newest_two = categories.observe(cfg, "wwr", runs=2)
    assert len(newest_two) == 2

    newest_ids = {p.posting_id for p in newest_two}
    assert newest_ids == {
        "https://example.test/20260819T120000",
        "https://example.test/20260818T120000",
    }

    # A posting written only in the oldest run must be absent from a window
    # that excludes it.
    oldest_only = "https://example.test/20260810T120000"
    assert oldest_only not in newest_ids

    all_ten = categories.observe(cfg, "wwr", runs=9)
    assert len(all_ten) == 9
    assert oldest_only not in {p.posting_id for p in all_ten}

    full_window = categories.observe(cfg, "wwr", runs=10)
    assert len(full_window) == 10
    assert oldest_only in {p.posting_id for p in full_window}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Full-Stack Programming", "remote-full-stack-programming-jobs"),
        ("Back-End Programming", "remote-back-end-programming-jobs"),
        ("Management and Finance", "remote-management-and-finance-jobs"),
        ("Customer Support", "remote-customer-support-jobs"),
        # Measured exception: the literal kebab form is a dead 301 for this
        # one category; the live slug drops the connective instead.
        ("DevOps and Sysadmin", "remote-devops-sysadmin-jobs"),
    ],
)
def test_wwr_tokens_derive_from_the_category_name(name, expected):
    assert categories.VOCABULARY["wwr"].token_for(name) == expected


def test_a_category_with_no_feed_derives_no_token():
    assert categories.VOCABULARY["wwr"].token_for("All Other Remote") is None


def test_a_posting_title_drops_the_company_prefix(cfg):
    write_run(
        cfg, "wwr", "20260820T120000", "all",
        '<?xml version="1.0"?><rss><channel><item>'
        "<title>Sticker Mule: Software engineer</title>"
        "<link>https://example.test/a</link><category>Full-Stack Programming</category>"
        "</item></channel></rss>",
    )

    assert categories.observe(cfg, "wwr")[0].title == "Software engineer"

