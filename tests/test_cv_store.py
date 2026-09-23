"""profile.json on disk.

The same rules the master.tex editor kept, now for the profile: a backup exists
before any write, backups are never rewritten, a restore is itself a write, and
nothing invalid ever reaches the file.
"""
from __future__ import annotations

import copy
import json

import pytest
from conftest import load_fixture

from jobhunt.cv import model
from jobhunt.cv import store as cvstore


def profile(**changes) -> model.Profile:
    data = copy.deepcopy(load_fixture("cv/profile.json"))
    data["basics"].update(changes)
    return model.parse(data)


def test_there_is_nothing_to_read_before_the_first_save(cfg, cv_source):
    assert cvstore.read(cfg) is None


def test_the_profile_lives_next_to_the_master(cfg, cv_source):
    assert cvstore.profile_path(cfg) == cv_source / "profile.json"
    assert cvstore.master_pdf_path(cfg) == cv_source / "Master_CV.pdf"


def test_an_explicit_path_wins(cfg, cv_source, tmp_path):
    cfg.raw["tailoring"]["profile_json"] = str(tmp_path / "elsewhere.json")

    assert cvstore.profile_path(cfg) == tmp_path / "elsewhere.json"


def test_what_is_written_is_what_is_read(cfg, cv_source):
    cvstore.write(cfg, profile())

    assert cvstore.read(cfg).profile == profile()


def test_the_first_save_has_nothing_to_back_up(cfg, cv_source):
    assert cvstore.write(cfg, profile()) is None


def test_a_save_backs_up_the_previous_version_first(cfg, cv_source):
    cvstore.write(cfg, profile())
    backup = cvstore.write(cfg, profile(name="Ada King"))

    kept = model.parse(json.loads(backup.path.read_text(encoding="utf-8")))
    assert kept.basics.name == "Ada Lovelace"
    assert cvstore.read(cfg).profile.basics.name == "Ada King"


def test_saves_in_the_same_second_do_not_overwrite_each_other(cfg, cv_source):
    for name in ("A", "B", "C"):
        cvstore.write(cfg, profile(name=name))

    assert len(cvstore.backups(cfg)) == 2


def test_backups_are_listed_newest_first(cfg, cv_source):
    for name in ("A", "B", "C"):
        cvstore.write(cfg, profile(name=name))

    names = [
        model.parse(json.loads(b.path.read_text(encoding="utf-8"))).basics.name
        for b in cvstore.backups(cfg)
    ]
    assert names == ["B", "A"]


def test_restoring_takes_a_backup_of_its_own_first(cfg, cv_source):
    cvstore.write(cfg, profile(name="A"))
    cvstore.write(cfg, profile(name="B"))
    oldest = cvstore.backups(cfg)[-1]

    cvstore.restore(cfg, oldest.name)

    assert cvstore.read(cfg).profile.basics.name == "A"
    assert len(cvstore.backups(cfg)) == 2


def test_restoring_something_that_is_not_a_backup_is_refused(cfg, cv_source):
    with pytest.raises(cvstore.StoreError):
        cvstore.restore(cfg, "../../etc/passwd")


def test_a_broken_file_on_disk_is_reported_with_its_path(cfg, cv_source):
    (cv_source / "profile.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(cvstore.StoreError) as caught:
        cvstore.read(cfg)
    assert "profile.json" in str(caught.value)


def test_an_atomic_write_leaves_no_temporary_file(cfg, cv_source):
    cvstore.write(cfg, profile())

    assert sorted(p.name for p in cv_source.iterdir()) == ["profile.json"]


def test_a_backup_keeps_the_source_suffix(cfg, cv_source):
    (cv_source / "master.tex").write_text("x", encoding="utf-8")

    backup = cvstore.take_backup(cv_source / "master.tex", cv_source / "backups", "master")

    assert backup.name.startswith("master-") and backup.name.endswith(".tex")


def test_concurrent_saves_each_publish_a_whole_file(cfg, cv_source):
    import threading

    cvstore.write(cfg, profile())
    names = [f"Writer {n}" for n in range(8)]
    threads = [threading.Thread(target=cvstore.write, args=(cfg, profile(name=name))) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert cvstore.read(cfg).profile.basics.name in names
    kept = cvstore.backups(cfg)
    assert len(kept) == 8, "every writer found a file to back up, and no backup overwrote another"
    for backup in kept:
        model.parse(json.loads(backup.path.read_text(encoding="utf-8")))
    assert not [p for p in cv_source.iterdir() if p.name.endswith(".tmp")]
