"""Previews of the person's own details in a template, built once per pair."""
from __future__ import annotations

import copy

import pytest
from conftest import load_fixture

from jobhunt.cv import ats, model, preview, templates
from jobhunt.cv import store as cvstore


@pytest.fixture
def cv_source(tmp_path, cfg):
    folder = tmp_path / "CV_Source"
    folder.mkdir()
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(folder / "master.tex")
    return folder


class Recorder:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        if self.code == 0:
            (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return self.code, "Output written on cv.pdf (1 page, 1 bytes)." if self.code == 0 else "! broken"


def save(cfg, **basics):
    data = copy.deepcopy(load_fixture("cv/profile.json"))
    data["basics"].update(basics)
    cvstore.write(cfg, model.parse(data))


def test_before_a_profile_exists_previews_show_the_sample(cfg, cv_source):
    assert preview.profile_for(cfg).basics.name == "Alex Morgan"


def test_a_preview_is_built_once_and_then_served_from_the_cache(cfg, cv_source):
    save(cfg)
    runner = Recorder()

    first = preview.preview(cfg, "classic", runner=runner)
    second = preview.preview(cfg, "classic", runner=runner)

    assert first.ok and second.pdf == first.pdf
    assert len(runner.calls) == 2  # two LaTeX passes, one build


def test_changing_the_profile_builds_it_again(cfg, cv_source):
    save(cfg)
    runner = Recorder()
    preview.preview(cfg, "classic", runner=runner)

    save(cfg, name="Ada King")
    preview.preview(cfg, "classic", runner=runner)

    assert len(runner.calls) == 4


def test_a_failed_preview_says_why_and_is_not_cached(cfg, cv_source):
    save(cfg)
    runner = Recorder(code=1)

    result = preview.preview(cfg, "classic", runner=runner)

    assert not result.ok and "broken" in result.log
    assert not any(preview.cache_dir(cfg).rglob("cv.pdf"))


def test_an_uploaded_template_previews_inside_the_sandbox(cfg, cv_source):
    save(cfg)
    added = templates.add(cfg, "Mine", templates.get(cfg, "classic").text(), engine="lualatex")
    runner = Recorder()

    preview.preview(cfg, added.id, runner=runner)

    assert runner.calls[0][0].endswith("sandbox-exec")


def test_no_thumbnail_without_pdftoppm(monkeypatch):
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)

    assert preview.thumbnail(b"%PDF-1.7") is None


def test_the_ats_report_is_read_from_the_script_output(cfg, tmp_path):
    script = tmp_path / "ats_check.py"
    script.write_text("", encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(script)

    def runner(argv, cwd):
        assert (cwd / "cv.tex").exists() and (cwd / "cv.pdf").exists()
        return 1, "[PASS] text layer\n[FAIL] encoding clean: ligature\n[WARN] links in text: x\n"

    report = ats.check(cfg, "tex", b"%PDF", runner=runner)

    assert report.ran and report.failures == ["encoding clean: ligature"]
    assert report.warnings == ["links in text: x"]


def test_without_the_script_the_ats_check_is_skipped_and_says_so(cfg, tmp_path):
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(tmp_path / "missing.py")

    report = ats.check(cfg, "tex", b"%PDF")

    assert not report.ran and "missing.py" in report.note
