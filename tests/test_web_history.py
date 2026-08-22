"""The last few runs, kept on disk.

A run's state used to live only in memory, so restarting the server threw away
the phase strip, the rank report and the gate verdicts for work already done.
"""
from __future__ import annotations

from jobhunt.web import history as history_module


def a_run(run_id, phase="done", outcome="completed", shortlisted=2):
    return {
        "run_id": run_id,
        "started_at": f"{run_id}-start",
        "finished_at": f"{run_id}-end",
        "phase": phase,
        "outcome": outcome,
        "counters": {"jobs_total": 4213},
        "results": [{"below_bar": False}] * shortlisted + [{"below_bar": True}],
    }


def test_a_saved_run_comes_back_whole(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    body = history_module.load(cfg, "20260822-100000")

    assert body["phase"] == "done"
    assert body["counters"]["jobs_total"] == 4213


def test_the_listing_is_newest_first_and_counts_only_what_cleared_the_bar(cfg) -> None:
    for run_id in ("20260820-090000", "20260822-100000", "20260821-090000"):
        history_module.save(cfg, run_id, a_run(run_id))

    rows = history_module.listing(cfg)

    assert [row["run_id"] for row in rows] == [
        "20260822-100000", "20260821-090000", "20260820-090000",
    ]
    assert rows[0]["shortlisted"] == 2, "near misses are not shortlisted jobs"


def test_only_the_newest_few_are_kept(cfg) -> None:
    for index in range(history_module.KEEP + 3):
        history_module.save(cfg, f"2026082{index}-090000", a_run(f"2026082{index}-090000"))

    rows = history_module.listing(cfg)

    assert len(rows) == history_module.KEEP
    assert history_module.load(cfg, "20260820-090000") is None, "the oldest was pruned"


def test_a_paused_run_is_marked_resumable(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000", phase="paused", outcome=None))

    assert history_module.listing(cfg)[0]["resumable"] is True


def test_a_finished_run_is_not_resumable(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    assert history_module.listing(cfg)[0]["resumable"] is False


def test_a_run_id_from_a_url_cannot_reach_outside_the_folder(cfg) -> None:
    """The id arrives in a path, so it is not trusted to be an id."""
    assert history_module.load(cfg, "../../../../etc/passwd") is None
    assert history_module.load(cfg, "") is None
    assert history_module.load(cfg, ".ssh") is None


def test_a_half_written_file_does_not_break_the_listing(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))
    (history_module.directory(cfg) / "20260821-090000.json").write_text("{ broken", encoding="utf-8")

    rows = history_module.listing(cfg)

    assert [row["run_id"] for row in rows] == ["20260822-100000"]


def test_a_reader_never_sees_half_a_write(cfg) -> None:
    """The write lands via a rename, so an interrupted save leaves the previous
    state rather than a truncated one."""
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    leftovers = list(history_module.directory(cfg).glob("*.writing"))

    assert leftovers == []


def test_nothing_saved_yet_is_an_empty_listing_not_a_failure(cfg) -> None:
    assert history_module.listing(cfg) == []
