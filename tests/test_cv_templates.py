"""The template library, and Classic in particular.

Classic is the template the tailoring skill's rules were written against: its
preamble is frozen byte-for-byte, and its body uses the macros the skill's cut
order names. Both are pinned here so neither drifts by accident.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import shutil
import subprocess

import pytest
from conftest import FIXTURES, load_fixture

from jobhunt import store as jobstore
from jobhunt.cv import latex, model, render, templates
from jobhunt.db.session import session_scope

GOLDEN = FIXTURES / "cv" / "classic_golden.tex"


def fixture_profile() -> model.Profile:
    return model.parse(copy.deepcopy(load_fixture("cv/profile.json")))


def classic_render(cfg) -> str:
    return render.render(fixture_profile(), templates.get(cfg, "classic").text())


def add_user_template(cfg, template_id: str, name: str, source: str | None = None) -> pathlib.Path:
    folder = templates.user_dir(cfg) / template_id
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    body = source if source is not None else templates.get(cfg, "classic").text()
    (folder / templates.SOURCE_NAME).write_text(body, encoding="utf-8")
    return folder


def test_classic_ships_with_the_package(cfg):
    classic = templates.get(cfg, "classic")

    assert classic.builtin and classic.name == "Classic" and classic.engine == "lualatex"
    assert "classic" in [t.id for t in templates.all_templates(cfg)]


def test_an_unknown_template_is_refused_by_name(cfg):
    with pytest.raises(templates.TemplateError) as caught:
        templates.get(cfg, "nope")
    assert "nope" in str(caught.value)


def test_the_default_is_classic_until_something_else_is_chosen(cfg):
    assert templates.default_id(cfg) == "classic"


def test_a_stored_default_that_no_longer_exists_falls_back_to_classic(cfg):
    with session_scope(cfg.db_path) as session:
        jobstore.meta_set(session, templates.DEFAULT_KEY, "deleted-one")

    assert templates.default_id(cfg) == "classic"


def test_a_user_template_is_listed_but_cannot_shadow_a_builtin(cfg):
    add_user_template(cfg, "mine", "Mine")
    add_user_template(cfg, "classic", "Impostor")

    assert templates.get(cfg, "mine").source == "upload"
    assert templates.get(cfg, "classic").name == "Classic"
    assert [t.id for t in templates.all_templates(cfg)].count("classic") == 1


def test_classic_keeps_the_master_preamble_byte_for_byte(cfg):
    preamble, _ = latex.split_preamble(classic_render(cfg))

    assert preamble == (FIXTURES / "cv" / "master_preamble.tex").read_text(encoding="utf-8")


def test_classic_renders_the_fixture_exactly_as_reviewed(cfg):
    out = classic_render(cfg)
    if os.environ.get("JOBHUNT_UPDATE_GOLDEN"):
        GOLDEN.write_text(out, encoding="utf-8")

    assert out == GOLDEN.read_text(encoding="utf-8")


def test_classic_prints_the_gpa_inside_the_degree_line(cfg):
    assert "{Master of Science in Computer Science (GPA: 3.90/4.00)}" in classic_render(cfg)


def test_classic_uses_the_macros_the_tailoring_skill_cuts_by(cfg):
    out = classic_render(cfg)

    for macro in ("\\resumeSubheading", "\\resumeProjectHeading", "\\resumeItem{", "\\section{EXPERIENCE}"):
        assert macro in out


def test_classic_keeps_hidden_content_as_comments(cfg):
    out = classic_render(cfg)

    assert "        % \\resumeItem{Retired bullet kept for reference.}" in out
    assert "    % \\resumeSubheading" in out
    assert "      % {Old Mill}{2019}" in out


def test_classic_carries_the_variants_as_comments(cfg):
    out = classic_render(cfg)

    assert "    % \\textbf{\\Huge Ada Lovelace - Optimization \\& Backend Engineer}" in out
    assert "% Energy-first summary.\n% Second line." in out


@pytest.mark.skipif(shutil.which("lualatex") is None, reason="lualatex is not installed")
def test_classic_compiles_and_prints_only_what_is_visible(cfg):
    built = latex.build(classic_render(cfg))

    assert built.ok, built.log
    if shutil.which("pdftotext") is None:
        return
    pdf = pathlib.Path(cfg.db_path).parent / "classic.pdf"
    pdf.write_bytes(built.pdf)
    text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
    assert "Özyeğin University" in text and "6 models × 36" in text
    assert "Retired bullet" not in text and "Old Mill" not in text


def test_classic_prints_a_skills_note_above_its_line(cfg):
    out = classic_render(cfg)

    assert (
        "     % ADDED: only claim what you can defend in an interview.\n     \\textbf{AI \\& ML}" in out
    )


MODERN_GOLDEN = FIXTURES / "cv" / "modern_golden.tex"


def test_modern_ships_alongside_classic(cfg):
    assert [t.id for t in templates.all_templates(cfg) if t.builtin] == ["classic", "modern"]
    assert templates.get(cfg, "modern").trusted


def test_modern_renders_the_fixture_exactly_as_reviewed(cfg):
    out = render.render(fixture_profile(), templates.get(cfg, "modern").text())
    if os.environ.get("JOBHUNT_UPDATE_GOLDEN"):
        MODERN_GOLDEN.write_text(out, encoding="utf-8")

    assert out == MODERN_GOLDEN.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("lualatex") is None, reason="lualatex is not installed")
def test_modern_compiles_and_prints_only_what_is_visible(cfg):
    built = latex.build(render.render(fixture_profile(), templates.get(cfg, "modern").text()))

    assert built.ok, built.log
    assert built.missing == ()


def test_the_sample_profile_is_a_valid_profile():
    assert templates.sample_profile().basics.name == "Alex Morgan"


def test_an_upload_is_stored_under_a_fresh_id(cfg):
    added = templates.add(cfg, "My Resume", "\\VAR{basics.name}", engine="xelatex", original="orig")

    assert added.id.startswith("my-resume-") and added.name == "My Resume" and not added.trusted
    assert added.engine == "xelatex" and (added.folder / "original.tex").read_text() == "orig"


def test_a_template_name_must_be_given_and_short(cfg):
    with pytest.raises(templates.TemplateError):
        templates.add(cfg, "   ", "x", engine="lualatex")
    with pytest.raises(templates.TemplateError):
        templates.add(cfg, "x" * 61, "x", engine="lualatex")


def test_an_upload_can_be_renamed(cfg):
    added = templates.add(cfg, "Old", "x", engine="lualatex")

    assert templates.rename(cfg, added.id, "New").name == "New"


def test_built_ins_cannot_be_renamed_or_removed(cfg):
    with pytest.raises(templates.TemplateError):
        templates.rename(cfg, "classic", "Mine")
    with pytest.raises(templates.TemplateError):
        templates.remove(cfg, "modern")


def test_removing_an_upload_moves_it_aside_rather_than_deleting(cfg):
    added = templates.add(cfg, "Gone", "x", engine="lualatex")

    templates.remove(cfg, added.id)

    assert added.id not in [t.id for t in templates.all_templates(cfg)]
    assert any((templates.user_dir(cfg) / ".deleted").glob(f"{added.id}-*"))


def test_the_default_template_cannot_be_removed(cfg):
    added = templates.add(cfg, "Mine", "x", engine="lualatex")
    with session_scope(cfg.db_path) as session:
        jobstore.meta_set(session, templates.DEFAULT_KEY, added.id)

    with pytest.raises(templates.TemplateError) as caught:
        templates.remove(cfg, added.id)
    assert "default" in str(caught.value)
