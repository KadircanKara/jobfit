"""Whether a template is fit to hold the profile."""
from __future__ import annotations

import copy
import shutil

import pytest
from conftest import FIXTURES, load_fixture

from jobhunt.cv import model, templates, validate

GOOD = (FIXTURES / "cv" / "upload_template.tex").read_text(encoding="utf-8")
LEAKY = (FIXTURES / "cv" / "upload_leaky.tex").read_text(encoding="utf-8")


@pytest.fixture
def cv_source(tmp_path, cfg):
    folder = tmp_path / "CV_Source"
    folder.mkdir()
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(folder / "master.tex")
    cfg.raw["tailoring"]["ats_check"] = str(tmp_path / "ats_check.py")
    (tmp_path / "ats_check.py").write_text("", encoding="utf-8")
    return folder


def profile():
    return model.parse(copy.deepcopy(load_fixture("cv/profile.json")))


def passing(argv, cwd):
    (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
    return 0, "Output written on cv.pdf (1 page, 1 bytes)."


def clean_ats(argv, cwd):
    return 0, "[PASS] everything\n"


def check(cfg, source, runner=passing, ats_runner=clean_ats):
    return validate.validate(cfg, source, profile=profile(), runner=runner, ats_runner=ats_runner)


def test_a_template_that_follows_the_contract_passes(cfg, cv_source):
    findings = check(cfg, GOOD)

    assert findings.ok, findings.problems
    assert findings.pdf.startswith(b"%PDF") and "Ada Lovelace" in findings.tex


def test_both_built_ins_pass_their_own_validation(cfg, cv_source):
    for template_id in ("classic", "modern"):
        findings = check(cfg, templates.get(cfg, template_id).text())
        assert findings.ok, (template_id, findings.problems)


def test_a_template_that_prints_hidden_items_is_refused(cfg, cv_source):
    findings = check(cfg, LEAKY)

    assert any("hidden" in problem for problem in findings.problems)


def test_a_template_that_leaves_a_section_out_is_refused_with_the_words(cfg, cv_source):
    without_extras = GOOD.replace(
        "\\BLOCK{for x in extras}\n\\textbf{\\VAR{x.label}}: \\VAR{x.value|rich}\\par\n\\BLOCK{endfor}\n", ""
    )

    findings = check(cfg, without_extras)

    assert any("military" in problem and "completed" in problem for problem in findings.problems)


def test_a_template_that_does_not_compile_says_why(cfg, cv_source):
    def failing(argv, cwd):
        return 1, "! Undefined control sequence."

    findings = check(cfg, GOOD, runner=failing)

    assert any("does not compile" in problem and "Undefined" in problem for problem in findings.problems)


def test_a_template_that_cannot_be_filled_says_why(cfg, cv_source):
    findings = check(cfg, GOOD.replace("basics.name", "basics.nmae"))

    assert findings.problems and "nmae" in findings.problems[0]


def test_ats_failures_are_warnings_not_refusals(cfg, cv_source):
    def ligatures(argv, cwd):
        return 1, "[FAIL] encoding clean: ligature glyphs\n"

    findings = check(cfg, GOOD, ats_runner=ligatures)

    assert findings.ok and findings.warnings == ["ATS check: encoding clean: ligature glyphs"]


def test_a_template_that_loads_its_own_files_is_refused():
    assert validate.self_contained("\\includegraphics{photo.jpg}")
    assert validate.self_contained("\\input{sections/work}")
    assert validate.self_contained("\\input{/Users/me/.ssh/id_rsa}")


def test_commented_out_loads_are_ignored():
    assert validate.self_contained("% \\includegraphics{photo.jpg}\n") == []


@pytest.mark.skipif(shutil.which("kpsewhich") is None, reason="kpsewhich is not installed")
def test_loading_a_file_that_ships_with_tex_is_fine():
    assert validate.self_contained("\\input{glyphtounicode}") == []


def test_the_engine_comes_from_the_magic_comment():
    assert validate.engine_of("% !TEX program = xelatex\n\\documentclass{article}") == "xelatex"
    assert validate.engine_of("% !TEX program = context\n") == "lualatex"
    assert validate.engine_of("\\documentclass{article}") == "lualatex"


def test_the_probe_carries_the_marker_only_in_hidden_places():
    probe = validate.probe_profile()
    visible = validate.expected_words(probe)

    assert validate.SENTINEL not in visible
