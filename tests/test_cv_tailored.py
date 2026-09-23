"""The master each application folder is cut from, and how its CV is built.

Two things matter most. A job tailored in a template gets its own copy of the
master in that template, and everything after (the agent, the verifier, the
reviewer, the studio) reads that copy. And a tailored CV is always built in the
sandbox: its preamble may be an upload's, and its body was written by an agent
that read a posting from the internet.
"""
from __future__ import annotations

import json

import pytest
from conftest import LatexRecorder, passing, save_profile

from jobhunt.cv import tailored


def failing(argv, cwd):
    return 1, "! Undefined control sequence.\nl.12 \\resumeSubheadng"


# --- the snapshot -------------------------------------------------------------


def test_a_snapshot_needs_a_saved_profile(cfg, cv_source):
    with pytest.raises(tailored.TailoredError, match="profile"):
        tailored.snapshot(cfg, "classic", runner=passing)


def test_a_snapshot_is_the_profile_in_the_chosen_template(cfg, cv_source):
    save_profile(cfg)

    snap = tailored.snapshot(cfg, "modern", runner=passing)

    assert snap.template.id == "modern"
    assert "Ada Lovelace" in snap.tex and "\\begin{document}" in snap.tex


def test_an_unknown_template_is_refused(cfg, cv_source):
    save_profile(cfg)

    with pytest.raises(tailored.TailoredError, match="nope"):
        tailored.snapshot(cfg, "nope", runner=passing)


def test_a_template_that_does_not_build_is_refused_with_the_reason(cfg, cv_source):
    save_profile(cfg)

    with pytest.raises(tailored.TailoredError, match="resumeSubheadng"):
        tailored.snapshot(cfg, "classic", runner=failing)


def test_writing_a_snapshot_records_its_template(cfg, cv_source, tmp_path):
    save_profile(cfg)
    folder = tmp_path / "Acme - Engineer"
    folder.mkdir()

    path = tailored.write(folder, tailored.snapshot(cfg, "modern", runner=passing))

    assert path == folder / "master.tex" and "Ada Lovelace" in path.read_text(encoding="utf-8")
    record = json.loads((folder / "master.json").read_text(encoding="utf-8"))
    assert record == {"template_id": "modern", "template_name": "Modern", "engine": "lualatex"}


def test_a_folder_reads_its_own_master(cfg, cv_source, tmp_path):
    save_profile(cfg)
    folder = tmp_path / "Acme - Engineer"
    folder.mkdir()
    tailored.write(folder, tailored.snapshot(cfg, "classic", runner=passing))

    assert tailored.master_for(cfg, folder) == folder / "master.tex"


def test_a_folder_from_before_templates_reads_the_global_master(cfg, cv_source, tmp_path):
    folder = tmp_path / "Old - Folder"
    folder.mkdir()

    assert tailored.master_for(cfg, folder) == cv_source / "master.tex"


# --- building a tailored CV ---------------------------------------------------


def tailored_folder(tmp_path, engine: str | None = None):
    folder = tmp_path / "Acme - Engineer"
    folder.mkdir()
    (folder / "cv.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    if engine:
        (folder / "master.json").write_text(json.dumps({"template_id": "t", "engine": engine}))
    return folder


def test_the_engine_comes_from_the_folders_template(tmp_path):
    folder = tailored_folder(tmp_path, engine="xelatex")

    assert tailored.engine_for(folder / "cv.tex") == "xelatex"


def test_a_folder_with_no_record_builds_with_lualatex(tmp_path):
    assert tailored.engine_for(tailored_folder(tmp_path) / "cv.tex") == "lualatex"


def test_an_engine_the_app_does_not_run_falls_back(tmp_path):
    folder = tailored_folder(tmp_path, engine="context")

    assert tailored.engine_for(folder / "cv.tex") == "lualatex"


def test_a_tailored_cv_always_builds_in_the_sandbox(cfg, cv_source, tmp_path):
    folder = tailored_folder(tmp_path, engine="lualatex")
    runner = LatexRecorder()

    built = tailored.build(cfg, folder / "cv.tex", runner=runner)

    assert built.ok
    assert runner.calls and all(argv[0].endswith("sandbox-exec") for argv in runner.calls)


def test_compiling_in_place_leaves_the_pdf_and_the_log(cfg, cv_source, tmp_path):
    folder = tailored_folder(tmp_path)

    def run(argv, cwd):
        (cwd / "cv.log").write_text("This is LuaHBTeX\nall good\n", encoding="utf-8")
        return passing(argv, cwd)

    built = tailored.compile_here(cfg, folder / "cv.tex", runner=run)

    assert built.ok
    assert (folder / "cv.pdf").read_bytes().startswith(b"%PDF")
    assert "all good" in (folder / "cv.log").read_text(encoding="utf-8")


def test_a_failed_compile_leaves_no_old_pdf_to_verify(cfg, cv_source, tmp_path):
    folder = tailored_folder(tmp_path)
    (folder / "cv.pdf").write_bytes(b"%PDF-1.7 from the last round")

    built = tailored.compile_here(cfg, folder / "cv.tex", runner=failing)

    assert not built.ok
    assert not (folder / "cv.pdf").exists()


def test_the_command_line_says_how_the_build_went(cfg, cv_source, tmp_path, monkeypatch, capsys):
    folder = tailored_folder(tmp_path)
    monkeypatch.setattr(tailored.config_module, "load", lambda path=None: cfg)

    assert tailored.main([str(folder / "cv.tex")], runner=passing) == 0
    assert "1 page" in capsys.readouterr().out

    assert tailored.main([str(folder / "cv.tex")], runner=failing) == 1
    assert "resumeSubheadng" in capsys.readouterr().err


def test_the_command_line_refuses_a_missing_file(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tailored.config_module, "load", lambda path=None: cfg)

    assert tailored.main([str(tmp_path / "nothing.tex")]) == 2


def test_a_build_is_current_only_for_the_source_it_was_built_from(cfg, cv_source, tmp_path):
    folder = tailored_folder(tmp_path)
    tex = folder / "cv.tex"

    assert tailored.current_build(tex) is None
    tailored.compile_here(cfg, tex, runner=passing)
    assert tailored.current_build(tex) == (folder / "cv.pdf").read_bytes()

    tex.write_text("\\documentclass{article}\\begin{document}edited\\end{document}\n")
    assert tailored.current_build(tex) is None


def test_a_failed_build_is_never_current(cfg, cv_source, tmp_path):
    folder = tailored_folder(tmp_path)
    tailored.compile_here(cfg, folder / "cv.tex", runner=passing)

    tailored.compile_here(cfg, folder / "cv.tex", runner=failing)

    assert tailored.current_build(folder / "cv.tex") is None
    assert not (folder / "cv.build.json").exists()
