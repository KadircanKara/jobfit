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
from sqlalchemy import func, select

from jobhunt import preferences as prefs_module
from jobhunt.db import models
from jobhunt.db.session import session_scope
from jobhunt.discovery import categories


def write_run(
    config, provider: str, run_key: str, token: str, payload: str | dict | list
) -> None:
    """Reproduce one raw payload envelope exactly as sync.py:218 writes it.

    `payload` is whatever the adapter's `fetch()` returned: a str of XML text
    for wwr, or an already-decoded dict/list for the JSON providers. Either
    shape round-trips through `json.dumps`/`json.loads` unchanged, which is
    exactly what `sync.py` and `categories.observe` do with it for real.
    """
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


@pytest.mark.parametrize(
    ("provider", "fixture_name"),
    [
        ("remotive", "remotive_all.json"),
        ("jobicy", "jobicy_all.json"),
        ("remoteok", "remoteok_all.json"),
    ],
)
def test_observe_reads_a_json_providers_real_decoded_payload_shape(
    cfg, provider, fixture_name
):
    """sync.py stores these providers' payload as a parsed dict/list, never a
    JSON string - their adapters' `fetch()` returns already-decoded data. A
    test that writes a string here would pass while missing the exact bug
    this fixed: `observe` must accept the real on-disk shape, not a
    convenient stand-in for it.
    """
    payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    write_run(cfg, provider, "20260820T120000", "all", payload)

    postings = categories.observe(cfg, provider)

    assert postings, f"the {fixture_name} fixture should yield postings"
    labels = {label for posting in postings for label in posting.labels}
    assert labels, f"the {fixture_name} fixture should yield labels"


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


def make_feed(rows: list[tuple[str, str, str]]) -> str:
    """(link, title, category) -> a minimal wwr feed."""
    items = "".join(
        f"<item><title>Co: {title}</title><link>{link}</link>"
        f"<category>{category}</category></item>"
        for link, title, category in rows
    )
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'


@pytest.fixture
def two_categories(cfg):
    # Deliberately built so rate and absolute count DISAGREE: Narrow wins on
    # rate (2 of 2), Broad wins on absolute matches (3 of 61, just under 5%).
    # This is the Full-Stack versus Back-End finding, kept as a regression.
    # Broad is held just under 5% (not ~10%) so the regression also catches a
    # minimum-rate filter in the 5-13% band, which is exactly the band that
    # would silently drop the real "Full-Stack Programming" category
    # (measured at 13% live) - the one category this feature exists to catch.
    rows = [(f"https://x.test/n{i}", "Python Developer", "Narrow") for i in range(2)]
    rows += [(f"https://x.test/b{i}", "Python Developer", "Broad") for i in range(3)]
    rows += [(f"https://x.test/o{i}", "Massage Therapist", "Broad") for i in range(58)]
    write_run(cfg, "wwr", "20260820T120000", "all", make_feed(rows))
    return cfg


@pytest.fixture
def python_prefs():
    return prefs_module.Preferences(titles=["Python Developer"])


def test_proposals_are_ordered_by_absolute_matches_not_rate(two_categories, python_prefs):
    proposals = categories.propose(two_categories, python_prefs)

    assert [p.category for p in proposals][:2] == ["Broad", "Narrow"], (
        "Broad has the lower rate (3/30 vs 2/2) and the higher absolute count"
    )


def test_a_proposal_reports_its_sample_size(two_categories, python_prefs):
    broad = next(p for p in categories.propose(two_categories, python_prefs) if p.category == "Broad")

    assert (broad.matched, broad.sample) == (3, 61)


def test_a_category_nothing_matched_is_not_proposed(two_categories, python_prefs):
    write_run(
        two_categories, "wwr", "20260821T120000", "all",
        make_feed([("https://x.test/z", "Dental Hygienist", "Dentistry")]),
    )

    assert "Dentistry" not in [p.category for p in categories.propose(two_categories, python_prefs)]


def test_a_category_with_no_token_is_still_proposed_but_unresolvable(cfg, python_prefs):
    write_run(
        cfg, "wwr", "20260820T120000", "all",
        make_feed([("https://x.test/a", "Python Developer", "All Other Remote")]),
    )

    proposal = categories.propose(cfg, python_prefs)[0]

    assert proposal.category == "All Other Remote"
    assert proposal.token is None, "shown so the user knows why it is not offered"


def test_no_titles_means_no_proposals(two_categories):
    assert categories.propose(two_categories, prefs_module.Preferences(titles=[])) == []


def test_proposing_writes_no_boards(two_categories, python_prefs):
    categories.propose(two_categories, python_prefs)

    with session_scope(two_categories.db_path) as session:
        assert session.execute(select(func.count(models.Board.id))).scalar_one() == 0



def test_approving_creates_a_board_the_sync_loop_will_pick_up(cfg):
    from sqlalchemy import select

    from jobhunt.db import models
    from jobhunt.db.session import session_scope

    assert categories.approve(cfg, [("wwr", "remote-full-stack-programming-jobs")]) == 1

    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == "remote-full-stack-programming-jobs")
        ).one()
        assert board.provider == "wwr"
        assert board.discovered_via == "feed"
        assert board.status == "candidate", "a guess, so one failed fetch kills it"


def test_approving_the_same_category_twice_is_idempotent(cfg):
    categories.approve(cfg, [("wwr", "remote-design-jobs")])
    categories.approve(cfg, [("wwr", "remote-design-jobs")])

    from sqlalchemy import func, select

    from jobhunt.db import models
    from jobhunt.db.session import session_scope

    with session_scope(cfg.db_path) as session:
        assert session.execute(select(func.count(models.Board.id))).scalar_one() == 1


def test_approving_a_category_the_seed_already_registered_is_a_no_op(cfg):
    from jobhunt.discovery import feeds

    feeds.seed(cfg)
    before = categories.approve(cfg, [("wwr", "remote-programming-jobs")])

    from sqlalchemy import func, select

    from jobhunt.db import models
    from jobhunt.db.session import session_scope

    with session_scope(cfg.db_path) as session:
        rows = session.execute(
            select(func.count(models.Board.id)).where(
                models.Board.token == "remote-programming-jobs"
            )
        ).scalar_one()
    assert (before, rows) == (1, 1)


def test_retiring_marks_the_board_dead_rather_than_deleting_it(cfg):
    from sqlalchemy import select

    from jobhunt.db import models
    from jobhunt.db.session import session_scope

    categories.approve(cfg, [("wwr", "remote-design-jobs")])

    assert categories.retire(cfg, [("wwr", "remote-design-jobs")]) == 1

    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == "remote-design-jobs")
        ).one()
        assert board.status == "dead", "PLAN.md section 6: rows are never deleted"


def test_retiring_something_that_was_never_approved_changes_nothing(cfg):
    assert categories.retire(cfg, [("wwr", "remote-design-jobs")]) == 0


def test_approving_revives_a_previously_retired_board(cfg):
    """A one-way door would be a real bug: retire something by mistake and it
    could never come back through approve, only by hand in the database."""
    from sqlalchemy import select

    from jobhunt.db import models
    from jobhunt.db.session import session_scope

    categories.approve(cfg, [("wwr", "remote-design-jobs")])
    categories.retire(cfg, [("wwr", "remote-design-jobs")])

    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == "remote-design-jobs")
        ).one()
        board.consecutive_errors = 3
        session.flush()

    assert categories.approve(cfg, [("wwr", "remote-design-jobs")]) == 1

    with session_scope(cfg.db_path) as session:
        board = session.scalars(
            select(models.Board).where(models.Board.token == "remote-design-jobs")
        ).one()
        assert board.status == "candidate"
        assert board.consecutive_errors == 0
