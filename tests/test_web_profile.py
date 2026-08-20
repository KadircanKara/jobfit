"""master.tex, edited in place.

The master is the only source of truth every tailored CV is cut from, so the
rules here are about not losing it: a backup exists before any write, backups
are never rewritten, and a compile that fails leaves the saved file alone.
"""
from __future__ import annotations

import pytest

from jobhunt.web import profile as profile_module

SAMPLE = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"


@pytest.fixture
def master(tmp_path, cfg):
    path = tmp_path / "CV_Source" / "master.tex"
    path.parent.mkdir(parents=True)
    path.write_text(SAMPLE, encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(path)
    return path


def test_reading_gives_back_what_is_on_disk(cfg, master):
    assert profile_module.read(cfg).text == SAMPLE


def test_a_write_backs_up_the_previous_version_first(cfg, master):
    profile_module.write(cfg, SAMPLE.replace("Hello", "Goodbye"))

    backups = profile_module.backups(cfg)
    assert len(backups) == 1
    assert backups[0].path.read_text(encoding="utf-8") == SAMPLE


def test_the_new_text_lands_in_the_master(cfg, master):
    profile_module.write(cfg, SAMPLE.replace("Hello", "Goodbye"))

    assert "Goodbye" in master.read_text(encoding="utf-8")


def test_backups_accumulate_rather_than_overwrite(cfg, master):
    profile_module.write(cfg, "one")
    profile_module.write(cfg, "two")

    assert len(profile_module.backups(cfg)) == 2


def test_backups_are_listed_newest_first(cfg, master):
    profile_module.write(cfg, "one")
    profile_module.write(cfg, "two")

    newest, older = profile_module.backups(cfg)
    assert newest.taken_at >= older.taken_at


def test_restoring_a_backup_also_takes_one_first(cfg, master):
    """Restoring is a write. Undoing it must stay possible."""
    profile_module.write(cfg, "edited")
    original = profile_module.backups(cfg)[0]

    profile_module.restore(cfg, original.name)

    assert master.read_text(encoding="utf-8") == SAMPLE
    assert len(profile_module.backups(cfg)) == 2


def test_restoring_something_that_is_not_a_backup_is_refused(cfg, master):
    with pytest.raises(profile_module.ProfileError):
        profile_module.restore(cfg, "../../etc/passwd")


def test_an_empty_master_is_refused(cfg, master):
    """A blank master would silently break every future tailoring run."""
    with pytest.raises(profile_module.ProfileError):
        profile_module.write(cfg, "   ")

    assert master.read_text(encoding="utf-8") == SAMPLE


def test_a_failed_compile_reports_the_log_and_leaves_the_file_alone(cfg, master):
    result = profile_module.compile_tex(cfg, runner=_failing_runner)

    assert result.ok is False
    assert "Undefined control sequence" in result.log
    assert master.read_text(encoding="utf-8") == SAMPLE


def test_a_successful_compile_returns_a_pdf(cfg, master):
    result = profile_module.compile_tex(cfg, runner=_passing_runner)

    assert result.ok is True
    assert result.pdf_bytes.startswith(b"%PDF")


def _failing_runner(tex_path, out_dir):
    return 1, "! Undefined control sequence.\nl.4 \\nope", None


def _passing_runner(tex_path, out_dir):
    pdf = out_dir / "master.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    return 0, "Output written on master.pdf (2 pages).", pdf
