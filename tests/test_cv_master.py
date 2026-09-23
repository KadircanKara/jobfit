"""The master CV: generated, never hand-edited, and in exactly one of three states.

The files in CV_Source are replaced only by a generate that compiled. A template
change, a failed build or a broken template leave them alone, because tailoring
and ranking keep reading master.tex and must keep reading the last good one.
"""
from __future__ import annotations

import copy
import json

import pytest
from conftest import load_fixture, passing

from jobhunt.cv import master, model, templates
from jobhunt.cv import store as cvstore


def save(cfg, **basics) -> None:
    data = copy.deepcopy(load_fixture("cv/profile.json"))
    data["basics"].update(basics)
    cvstore.write(cfg, model.parse(data))


def failing(argv, cwd):
    return 1, "! Undefined control sequence."


def add_template(cfg, template_id: str, name: str, source: str | None = None) -> None:
    folder = templates.user_dir(cfg) / template_id
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    body = source if source is not None else templates.get(cfg, "classic").text()
    (folder / templates.SOURCE_NAME).write_text(body, encoding="utf-8")


def master_backups(folder, prefix: str) -> list:
    backups = folder / "backups"
    return sorted(backups.glob(f"{prefix}-*")) if backups.is_dir() else []


def test_before_any_generate_it_is_empty(cfg, cv_source):
    save(cfg)

    status = master.status(cfg)
    assert status.state == "empty" and status.reason == "Nothing has been generated yet."
    assert status.template_id == "classic" and status.template_name == "Classic"


def test_a_generate_writes_both_files_and_is_ready(cfg, cv_source):
    save(cfg)

    outcome = master.generate(cfg, runner=passing)

    assert outcome.ok and outcome.pages == 1 and outcome.status.state == "ready"
    assert "\\resumeSubheading" in (cv_source / "master.tex").read_text(encoding="utf-8")
    assert (cv_source / "Master_CV.pdf").read_bytes() == b"%PDF-1.7 fake"
    assert outcome.status.generated_at


def test_saving_the_profile_afterwards_makes_it_stale_but_still_downloadable(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)

    save(cfg, name="Ada King")

    assert master.status(cfg).state == "stale"
    assert master.download(cfg, "pdf") == cv_source / "Master_CV.pdf"


def test_a_failed_build_writes_nothing(cfg, cv_source):
    save(cfg)

    outcome = master.generate(cfg, runner=failing)

    assert not outcome.ok and "Undefined control sequence" in outcome.log
    assert not (cv_source / "master.tex").exists()
    assert outcome.status.state == "empty"


def test_a_failed_build_leaves_the_last_good_master_in_place(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)
    save(cfg, name="Ada King")

    master.generate(cfg, runner=failing)

    assert "Ada Lovelace" in (cv_source / "master.tex").read_text(encoding="utf-8")
    assert master.status(cfg).state == "stale"


def test_changing_the_template_empties_it_and_touches_no_file(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)
    before = (cv_source / "master.tex").read_bytes()
    add_template(cfg, "plain", "Plain")

    status = master.use_template(cfg, "plain")

    assert status.state == "empty" and "Plain" in status.reason
    assert (cv_source / "master.tex").read_bytes() == before
    with pytest.raises(master.MasterError):
        master.download(cfg, "pdf")
    with pytest.raises(master.MasterError):
        master.download(cfg, "tex")


def test_switching_back_to_the_old_template_stays_empty(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)
    add_template(cfg, "plain", "Plain")

    master.use_template(cfg, "plain")
    status = master.use_template(cfg, "classic")

    assert status.state == "empty"


def test_generating_in_the_new_template_makes_it_ready_again(cfg, cv_source):
    save(cfg)
    add_template(cfg, "plain", "Plain")
    master.use_template(cfg, "plain")

    outcome = master.generate(cfg, runner=passing)

    assert outcome.status.state == "ready" and outcome.status.template_id == "plain"


def test_an_unknown_template_is_refused(cfg, cv_source):
    with pytest.raises(templates.TemplateError):
        master.use_template(cfg, "nope")


def test_a_master_edited_by_hand_reads_as_empty(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)
    with (cv_source / "master.tex").open("a", encoding="utf-8") as handle:
        handle.write("% edited by hand\n")

    status = master.status(cfg)
    assert status.state == "empty" and "outside the app" in status.reason


def test_a_generate_backs_up_the_master_it_replaces(cfg, cv_source):
    (cv_source / "master.tex").write_text("the hand-written master", encoding="utf-8")
    save(cfg)

    master.generate(cfg, runner=passing)

    kept = master_backups(cv_source, "master")
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "the hand-written master"


def test_an_unchanged_master_is_not_backed_up_or_rewritten_again(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)

    master.generate(cfg, runner=passing)

    assert master_backups(cv_source, "master") == []
    assert master_backups(cv_source, "Master_CV") == []


def test_generating_without_a_profile_is_refused(cfg, cv_source):
    with pytest.raises(master.MasterError):
        master.generate(cfg, runner=passing)


def test_a_template_that_does_not_render_is_a_failed_generate(cfg, cv_source):
    save(cfg)
    add_template(cfg, "broken", "Broken", source="\\VAR{nope}")
    master.use_template(cfg, "broken")

    outcome = master.generate(cfg, runner=passing)

    assert not outcome.ok and "nope" in outcome.log
    assert not (cv_source / "master.tex").exists()


def test_downloads_are_refused_while_empty(cfg, cv_source):
    save(cfg)

    with pytest.raises(master.MasterError) as caught:
        master.download(cfg, "tex")
    assert "generated" in str(caught.value)


def test_a_run_that_died_between_the_two_writes_does_not_leave_an_old_pdf(cfg, cv_source):
    save(cfg)
    master.generate(cfg, runner=passing)
    save(cfg, name="Ada King")
    # As if the last generate wrote the new master.tex and died before the PDF.
    newer = master.render.render(cvstore.read(cfg).profile, templates.get(cfg, "classic").text())
    (cv_source / "master.tex").write_text(newer, encoding="utf-8")

    def second_build(argv, cwd):
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 second")
        return 0, "Output written on cv.pdf (1 page, 15 bytes)."

    master.generate(cfg, runner=second_build)

    assert (cv_source / "Master_CV.pdf").read_bytes() == b"%PDF-1.7 second"
    assert master.status(cfg).state == "ready"


def test_a_second_generate_while_one_runs_is_refused(cfg, cv_source):
    save(cfg)

    with master._LOCK, pytest.raises(master.MasterError) as caught:
        master.generate(cfg, runner=passing)
    assert "already" in str(caught.value)


def test_characters_the_font_could_not_print_reach_the_outcome(cfg, cv_source):
    save(cfg)

    def lossy(argv, cwd):
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        line = "Missing character: There is no ✓ (U+2713) in font x!\n"
        (cwd / "cv.log").write_text(line, encoding="utf-8")
        return 0, "Output written on cv.pdf (1 page, 13 bytes)."

    assert master.generate(cfg, runner=lossy).missing == ("✓",)
