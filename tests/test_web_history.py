"""The last few runs, kept on disk.

A run's state used to live only in memory, so restarting the server threw away
the phase strip, the rank report and the gate verdicts for work already done.
"""
from __future__ import annotations

import pytest

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


# --- names and deletion ----------------------------------------------------------


def test_a_named_run_carries_its_name_in_the_listing(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    history_module.rename(cfg, "20260822-100000", "  Berlin push  ")

    assert history_module.listing(cfg)[0]["name"] == "Berlin push"


def test_an_unnamed_run_lists_no_name(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    assert history_module.listing(cfg)[0]["name"] is None


def test_a_blank_name_clears_it(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))
    history_module.rename(cfg, "20260822-100000", "Berlin push")

    history_module.rename(cfg, "20260822-100000", "   ")

    assert history_module.listing(cfg)[0]["name"] is None


def test_a_name_survives_the_run_being_saved_again(cfg) -> None:
    # A live run rewrites its file on every phase change; the name must not be
    # part of what it overwrites.
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))
    history_module.rename(cfg, "20260822-100000", "Berlin push")

    history_module.save(cfg, "20260822-100000", a_run("20260822-100000", shortlisted=3))

    assert history_module.listing(cfg)[0]["name"] == "Berlin push"


def test_renaming_a_run_that_is_not_on_file_is_refused(cfg) -> None:
    with pytest.raises(LookupError):
        history_module.rename(cfg, "20260822-100000", "Berlin push")


def test_a_name_longer_than_the_limit_is_refused(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))

    with pytest.raises(ValueError):
        history_module.rename(cfg, "20260822-100000", "x" * (history_module.NAME_MAX + 1))


def test_a_deleted_run_is_gone_from_the_listing_and_its_name_with_it(cfg) -> None:
    history_module.save(cfg, "20260821-090000", a_run("20260821-090000"))
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))
    history_module.rename(cfg, "20260822-100000", "Berlin push")

    assert history_module.delete(cfg, "20260822-100000") is True

    assert [row["run_id"] for row in history_module.listing(cfg)] == ["20260821-090000"]
    assert history_module.load(cfg, "20260822-100000") is None
    assert "20260822-100000" not in history_module.names(cfg)


def test_deleting_a_run_that_is_not_on_file_reports_false(cfg) -> None:
    assert history_module.delete(cfg, "20260822-100000") is False


def test_delete_cannot_reach_outside_the_folder(cfg) -> None:
    outside = history_module.directory(cfg).parent / "keep.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("{}", encoding="utf-8")

    assert history_module.delete(cfg, "../keep") is False
    assert outside.exists()


def test_the_names_file_is_never_listed_or_pruned_as_a_run(cfg) -> None:
    history_module.save(cfg, "20260822-100000", a_run("20260822-100000"))
    history_module.rename(cfg, "20260822-100000", "Berlin push")
    for day in range(10, 16):
        history_module.save(cfg, f"202608{day}-090000", a_run(f"202608{day}-090000"))

    rows = history_module.listing(cfg)

    assert len(rows) == history_module.KEEP
    assert all(row["run_id"].startswith("2026") for row in rows)
    assert history_module.names(cfg) == {"20260822-100000": "Berlin push"}


def test_pruning_a_run_drops_its_name(cfg) -> None:
    history_module.save(cfg, "20260801-090000", a_run("20260801-090000"))
    history_module.rename(cfg, "20260801-090000", "Oldest")
    for day in range(10, 16):
        history_module.save(cfg, f"202608{day}-090000", a_run(f"202608{day}-090000"))

    assert "20260801-090000" not in history_module.names(cfg)
