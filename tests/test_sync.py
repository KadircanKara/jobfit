"""End-to-end pipeline test with no network.

Payloads are placed in data/raw by hand and normalization is run over them, which
is exactly what `sync --from-raw` does. If this passes, the raw -> normalized ->
deduped -> stored path works independently of any endpoint being up.
"""
from __future__ import annotations

import datetime as dt_module
import json

from conftest import load_fixture

from jobhunt import store, sync
from jobhunt.db.models import Board, Company, Job, Run, utcnow
from jobhunt.db.session import session_scope
from jobhunt.sources.base import JobPosting


def place_raw(cfg, source: str, token: str, run_key: str, payload: dict) -> None:
    directory = sync.raw_dir(cfg, source, run_key)
    directory.mkdir(parents=True, exist_ok=True)
    envelope = {
        "run_key": run_key, "source": source, "provider": source, "token": token,
        "market": "global_remote", "board_id": None, "fetched_at": "2026-08-20T00:00:00",
        "payload": payload,
    }
    (directory / f"{source}__{token}.json").write_text(json.dumps(envelope), encoding="utf-8")


def test_normalize_pass_stores_jobs_companies_and_boards(cfg) -> None:
    place_raw(cfg, "greenhouse", "stripe", "R1", load_fixture("greenhouse_stripe.json"))
    result = sync.normalize_pass(cfg, "greenhouse", "R1")

    assert result.normalized == 5
    assert result.new == 5
    with session_scope(cfg.db_path) as session:
        assert session.query(Job).count() == 5
        assert session.query(Company).count() == 1
        board = session.query(Board).one()
        assert (board.provider, board.token, board.status) == ("greenhouse", "stripe", "validated")
        assert board.next_fetch_at is not None


def test_second_pass_over_the_same_payload_creates_nothing_new(cfg) -> None:
    """Layer 1 dedupe: (source, external_id) never produces a second row."""
    place_raw(cfg, "ashby", "ramp", "R1", load_fixture("ashby_ramp.json"))
    first = sync.normalize_pass(cfg, "ashby", "R1")
    second = sync.normalize_pass(cfg, "ashby", "R1")

    assert first.new == 5
    assert (second.new, second.unchanged) == (0, 5)
    with session_scope(cfg.db_path) as session:
        assert session.query(Job).count() == 5


def test_changed_description_counts_as_updated_not_new(cfg) -> None:
    payload = load_fixture("ashby_ramp.json")
    place_raw(cfg, "ashby", "ramp", "R1", payload)
    sync.normalize_pass(cfg, "ashby", "R1")

    payload["jobs"][0]["descriptionHtml"] = "<p>" + ("rewritten body " * 60) + "</p>"
    payload["jobs"][0]["descriptionPlain"] = "rewritten body " * 60
    place_raw(cfg, "ashby", "ramp", "R2", payload)
    result = sync.normalize_pass(cfg, "ashby", "R2")

    assert (result.new, result.updated) == (0, 1)


def test_reparsing_stored_raw_never_needs_the_network(cfg) -> None:
    """A parser bug costs a re-normalize, never a re-fetch. Non-negotiable 2."""
    place_raw(cfg, "greenhouse", "stripe", "R1", load_fixture("greenhouse_stripe.json"))
    sync.normalize_pass(cfg, "greenhouse", "R1")
    again = sync.sync_source(cfg, "greenhouse", from_raw="R1")
    assert again.status == "ok"
    assert again.normalized == 5


def test_missing_job_deactivates_only_after_two_runs(cfg) -> None:
    """One flaky listing must never wipe a board. PLAN.md section 6."""
    payload = load_fixture("ashby_ramp.json")
    place_raw(cfg, "ashby", "ramp", "R1", payload)
    sync.normalize_pass(cfg, "ashby", "R1")
    dropped_id = payload["jobs"][0]["id"]

    trimmed = {"jobs": payload["jobs"][1:], "apiVersion": payload.get("apiVersion")}
    place_raw(cfg, "ashby", "ramp", "R2", trimmed)
    first = sync.normalize_pass(cfg, "ashby", "R2")
    assert first.deactivated == 0

    place_raw(cfg, "ashby", "ramp", "R3", trimmed)
    second = sync.normalize_pass(cfg, "ashby", "R3")
    assert second.deactivated == 1

    with session_scope(cfg.db_path) as session:
        job = session.query(Job).filter_by(source="ashby", external_id=dropped_id).one()
        assert job.is_active is False


def test_a_returning_job_is_counted_as_a_repost(cfg) -> None:
    payload = load_fixture("ashby_ramp.json")
    trimmed = {"jobs": payload["jobs"][1:]}
    place_raw(cfg, "ashby", "ramp", "R1", payload)
    sync.normalize_pass(cfg, "ashby", "R1")
    for run_key in ("R2", "R3"):
        place_raw(cfg, "ashby", "ramp", run_key, trimmed)
        sync.normalize_pass(cfg, "ashby", run_key)

    place_raw(cfg, "ashby", "ramp", "R4", payload)
    sync.normalize_pass(cfg, "ashby", "R4")
    with session_scope(cfg.db_path) as session:
        job = session.query(Job).filter_by(external_id=payload["jobs"][0]["id"]).one()
        assert job.is_active is True
        assert job.repost_count == 1


def test_cross_source_duplicate_gets_one_canonical_head(cfg) -> None:
    """The same role on two sources produces two rows and one cluster."""
    shared_description = "<p>" + ("Build and operate the payments API in Python. " * 20) + "</p>"
    with session_scope(cfg.db_path) as session:
        for source, external_id in (("greenhouse", "g1"), ("ashby", "a1")):
            store.upsert_posting(
                session,
                JobPosting(
                    source=source,
                    external_id=external_id,
                    market="global_remote",
                    title="Senior Backend Engineer" if source == "greenhouse" else "Backend Engineer",
                    company_name="Acme, Inc.",
                    company_domain="acme.com",
                    location_raw="Remote",
                    description_html=shared_description,
                    description_text=shared_description,
                ),
            )
    with session_scope(cfg.db_path) as session:
        from jobhunt.pipeline.dedupe import apply_clustering

        apply_clustering(session)
    with session_scope(cfg.db_path) as session:
        heads = {j.canonical_job_id for j in session.query(Job).all()}
        assert session.query(Job).count() == 2
        assert len(heads) == 1


def test_unknown_source_raises_with_the_valid_list(cfg) -> None:
    import pytest

    from jobhunt import sources

    with pytest.raises(KeyError, match="unknown source"):
        sources.get("nope")


def test_a_failing_source_degrades_and_does_not_raise(cfg, monkeypatch) -> None:
    """Source isolation. Non-negotiable 3."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("endpoint on fire")

    monkeypatch.setattr(sync, "due_boards", boom)
    result = sync.sync_source(cfg, "ashby")
    assert result.status == "failed"
    assert "endpoint on fire" in result.error_detail
    with session_scope(cfg.db_path) as session:
        assert session.query(Run).one().status == "failed"


def test_dry_run_writes_nothing(cfg) -> None:
    place_raw(cfg, "greenhouse", "stripe", "R1", load_fixture("greenhouse_stripe.json"))
    result = sync.normalize_pass(cfg, "greenhouse", "R1", dry_run=True)
    assert result.normalized == 5
    with session_scope(cfg.db_path) as session:
        assert session.query(Job).count() == 0


def seed_boards(cfg, *specs) -> None:
    """Put boards in the table the way discovery would, without discovery."""
    with session_scope(cfg.db_path) as session:
        for provider, token in specs:
            store.get_or_create_board(session, provider, token, "yc", "global_remote")


def test_due_boards_respects_next_fetch_at(cfg) -> None:
    import datetime as dt

    seed_boards(cfg, ("ashby", "ramp"), ("ashby", "openai"))
    assert len(sync.due_boards(cfg, "ashby", force=False, limit=100)) == 2

    with session_scope(cfg.db_path) as session:
        for board in session.query(Board).filter_by(provider="ashby").all():
            board.next_fetch_at = dt.datetime(2099, 1, 1)

    assert sync.due_boards(cfg, "ashby", force=False, limit=100) == []
    assert len(sync.due_boards(cfg, "ashby", force=True, limit=100)) == 2


def test_per_run_board_cap_is_enforced(cfg) -> None:
    """No command ever iterates the full boards table. Non-negotiable 8."""
    seed_boards(cfg, ("ashby", "ramp"), ("ashby", "openai"))
    assert len(sync.due_boards(cfg, "ashby", force=True, limit=1)) == 1


def test_a_repeatedly_failing_board_is_marked_dead(cfg) -> None:
    """Three consecutive errors and the board stops costing a request. PLAN.md 3.5."""
    with session_scope(cfg.db_path) as session:
        board = store.get_or_create_board(session, "ashby", "gone", "fixture", "global_remote")
        # A board that has produced jobs before earns the three strikes. Candidates
        # do not, and that case has its own test below.
        board.status = "validated"

    for _ in range(sync.DEAD_AFTER_ERRORS - 1):
        assert sync.record_fetch_failures(cfg, "ashby", ["gone"]) == (0, 0)
    assert sync.record_fetch_failures(cfg, "ashby", ["gone"]) == (1, 0)

    with session_scope(cfg.db_path) as session:
        board = session.query(Board).filter_by(token="gone").one()
        assert board.status == "dead"
        assert board.next_fetch_at is None
    assert sync.due_boards(cfg, "ashby", force=True, limit=10) == []


def test_a_successful_fetch_resets_the_error_count(cfg) -> None:
    place_raw(cfg, "ashby", "ramp", "R1", load_fixture("ashby_ramp.json"))
    sync.record_fetch_failures(cfg, "ashby", ["ramp"])
    sync.normalize_pass(cfg, "ashby", "R1")
    with session_scope(cfg.db_path) as session:
        assert session.query(Board).filter_by(token="ramp").one().consecutive_errors == 0


def test_a_candidate_board_dies_on_its_first_failure(cfg) -> None:
    """A wrong guess costs exactly one request, not three. PLAN.md 3.5.

    Strategies C and D will produce tens of thousands of unvalidated tokens, so
    the cost of being wrong has to stay at one.
    """
    with session_scope(cfg.db_path) as session:
        store.get_or_create_board(session, "ashby", "notacompany", "domain_guess", "global_remote")

    # Reported as rejected, not as an error: the validation loop did its job.
    assert sync.record_fetch_failures(cfg, "ashby", ["notacompany"]) == (1, 1)
    with session_scope(cfg.db_path) as session:
        board = session.query(Board).filter_by(token="notacompany").one()
        assert board.status == "dead"
    assert sync.due_boards(cfg, "ashby", force=True, limit=10) == []


def test_a_run_of_rejected_guesses_is_not_degraded(cfg, monkeypatch) -> None:
    """Wrong guesses dying is the validation loop working, not a fault.

    If a normal discovery run reported "degraded", the status would stop meaning
    anything and cron would train the user to ignore it.
    """
    seed_boards(cfg, ("ashby", "nope1"), ("ashby", "nope2"))

    def fail_everything(config, source, refs, run_key):
        return 0, [ref.token for ref in refs], ["404" for _ in refs]

    monkeypatch.setattr(sync, "fetch_pass", fail_everything)
    result = sync.sync_source(cfg, "ashby")

    assert result.rejected == 2
    assert result.errors == 0
    assert result.dead_boards == 2
    assert result.status == "ok"


def test_a_validated_board_failing_still_degrades_the_run(cfg) -> None:
    seed_boards(cfg, ("ashby", "waslive"))
    with session_scope(cfg.db_path) as session:
        session.query(Board).filter_by(token="waslive").one().status = "validated"

    dead, rejected = sync.record_fetch_failures(cfg, "ashby", ["waslive"])
    assert (dead, rejected) == (0, 0)


def test_due_boards_filters_by_market(cfg) -> None:
    """A market is a separate pipeline with its own filters. PLAN.md section 2."""
    with session_scope(cfg.db_path) as session:
        store.get_or_create_board(session, "ashby", "ycco", "yc", "yc")
        store.get_or_create_board(session, "ashby", "remoteco", "manual", "global_remote")

    assert len(sync.due_boards(cfg, "ashby", force=True, limit=10)) == 2
    yc_only = sync.due_boards(cfg, "ashby", force=True, limit=10, market="yc")
    assert [ref.token for ref in yc_only] == ["ycco"]


def test_a_feed_board_is_refetched_daily_not_weekly(cfg) -> None:
    """An aggregator is where a job first appears, often days before the company
    board is next due. A weekly cadence would defeat the point of having one."""
    import datetime as dt

    from jobhunt.db.models import Board as BoardModel

    feed = BoardModel(provider="remotive", token="all", discovered_via="feed",
                      market="global_remote")
    ats = BoardModel(provider="ashby", token="acme", discovered_via="yc",
                     market="global_remote")
    sync._record_board_outcome(feed, 30)
    sync._record_board_outcome(ats, 30)

    assert (feed.next_fetch_at - feed.last_fetched_at) == dt.timedelta(
        days=sync.FEED_REFETCH_DAYS
    )
    assert (ats.next_fetch_at - ats.last_fetched_at) == dt.timedelta(days=7)


def test_a_backfill_of_candidates_never_starves_the_working_boards(cfg) -> None:
    """The failure this prevents: a Common Crawl backfill drops thousands of
    NULL next_fetch_at rows in at once. If candidates sorted first, every run
    for weeks would be spent on unvalidated guesses while the boards actually
    carrying jobs went stale.
    """
    import datetime as dt

    from jobhunt.db.models import Board as BoardModel

    past = utcnow() - dt.timedelta(days=1)
    with session_scope(cfg.db_path) as session:
        for index in range(5):
            session.add(BoardModel(provider="ashby", token=f"live{index}", discovered_via="yc",
                                   market="global_remote", status="validated", next_fetch_at=past))
        for index in range(100):
            session.add(BoardModel(provider="ashby", token=f"cand{index}",
                                   discovered_via="commoncrawl", market="global_remote",
                                   status="candidate", next_fetch_at=None))

    refs = sync.due_boards(cfg, "ashby", force=False, limit=20, candidate_limit=3)
    tokens = [ref.token for ref in refs]

    assert tokens[:5] == ["live0", "live1", "live2", "live3", "live4"]
    assert len([t for t in tokens if t.startswith("cand")]) == 3
    assert len(refs) == 8


def test_the_candidate_slice_is_capped_by_the_overall_run_cap(cfg) -> None:
    from jobhunt.db.models import Board as BoardModel

    with session_scope(cfg.db_path) as session:
        for index in range(20):
            session.add(BoardModel(provider="ashby", token=f"cand{index}",
                                   discovered_via="commoncrawl", market="global_remote",
                                   status="candidate", next_fetch_at=None))

    assert len(sync.due_boards(cfg, "ashby", force=False, limit=4, candidate_limit=50)) == 4


def test_a_second_writer_waits_instead_of_failing(cfg) -> None:
    """WAL allows one writer. A cron sync overlapping a manual one must queue,
    not blow up with "database is locked"."""
    from jobhunt.db.session import get_engine

    with get_engine(cfg.db_path).connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout >= 30000


def test_feed_boards_are_fetched_first_under_a_cap(cfg) -> None:
    """A fast pass with a small cap must still refresh the aggregators. They are
    where a job appears first, often days before the company board is due."""
    from jobhunt.db.models import Board as BoardModel

    with session_scope(cfg.db_path) as session:
        for index in range(10):
            session.add(BoardModel(provider="ashby", token=f"ats{index}", discovered_via="yc",
                                   market="global_remote", status="validated",
                                   next_fetch_at=utcnow() - dt_module.timedelta(days=1)))
        session.add(BoardModel(provider="ashby", token="feed", discovered_via="feed",
                               market="global_remote", status="validated",
                               next_fetch_at=utcnow() - dt_module.timedelta(hours=1)))

    refs = sync.due_boards(cfg, "ashby", force=False, limit=3)
    assert refs[0].token == "feed"


def test_a_second_sync_backs_off_while_one_is_running(cfg) -> None:
    """The fast pass and the background backfill must not fetch the same boards."""
    with sync.SyncLock(cfg) as first:
        assert first.acquired
        with sync.SyncLock(cfg) as second:
            assert not second.acquired
    # Released on exit, so the next run proceeds.
    with sync.SyncLock(cfg) as third:
        assert third.acquired


def test_a_stale_lock_does_not_block_forever(cfg) -> None:
    """A crashed run must not require manual cleanup."""
    lock = sync.SyncLock(cfg)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    old = utcnow() - dt_module.timedelta(seconds=sync.LOCK_STALE_SECONDS + 60)
    lock.path.write_text(f"999999 {old.isoformat()}\n", encoding="utf-8")

    with sync.SyncLock(cfg) as fresh:
        assert fresh.acquired
