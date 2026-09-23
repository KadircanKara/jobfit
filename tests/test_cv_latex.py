"""Building a CV PDF.

The runner is injected so these tests never start LaTeX. The one test that does
is opt-in and skips on a machine without lualatex.
"""
from __future__ import annotations

import shutil

import pytest

from jobhunt.cv import latex

DOC = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"


class Recorder:
    def __init__(self, code: int = 0, log: str = "Output written on cv.pdf (2 pages, 10 bytes).") -> None:
        self.code, self.log, self.calls = code, log, []

    def __call__(self, argv, cwd):
        self.calls.append((argv, cwd))
        if self.code == 0:
            (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return self.code, self.log


def test_a_build_runs_two_passes_and_returns_the_pdf():
    runner = Recorder()

    result = latex.build(DOC, runner=runner)

    assert result.ok and result.pdf.startswith(b"%PDF") and result.pages == 2
    assert len(runner.calls) == 2


def test_shell_escape_is_always_off_and_the_engine_comes_first():
    runner = Recorder()

    latex.build(DOC, engine="lualatex", runner=runner)

    argv = runner.calls[0][0]
    assert argv[0] == "lualatex"
    assert "-no-shell-escape" in argv and "-halt-on-error" in argv


def test_a_failed_first_pass_stops_and_reports_the_log():
    runner = Recorder(code=1, log="! Undefined control sequence.")

    result = latex.build(DOC, runner=runner)

    assert not result.ok and "Undefined control sequence" in result.log
    assert len(runner.calls) == 1


def test_an_unknown_engine_is_refused_without_running_anything():
    runner = Recorder()

    result = latex.build(DOC, engine="troff", runner=runner)

    assert not result.ok and "troff" in result.log and runner.calls == []


def test_a_missing_binary_is_a_failed_build_not_a_crash(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which", lambda name: None)

    result = latex.build(DOC)

    assert not result.ok and "not found" in result.log


def test_pages_fall_back_to_counting_page_objects():
    pdf = b"<< /Type /Pages >> << /Type /Page /X 1 >> << /Type /Page >>"

    assert latex.page_count("no summary line", pdf) == 2


def test_a_long_log_keeps_its_end():
    trimmed = latex.trim("a" * 100 + "the error", keep=9)

    assert trimmed.endswith("the error") and len(trimmed) < 20


def test_the_preamble_splits_where_verify_cv_splits_it():
    head = "\\documentclass{article}\n"
    assert latex.split_preamble(DOC) == (head, DOC[len(head):])
    assert latex.split_preamble("no document") == ("no document", "")


@pytest.mark.skipif(shutil.which("lualatex") is None, reason="lualatex is not installed")
def test_a_real_build_produces_a_one_page_pdf():
    result = latex.build(DOC)

    assert result.ok, result.log
    assert result.pdf.startswith(b"%PDF") and result.pages == 1


def test_lualatex_runs_without_sockets():
    runner = Recorder()

    latex.build(DOC, engine="lualatex", runner=runner)
    latex.build(DOC, engine="pdflatex", runner=runner)

    assert "--nosocket" in runner.calls[0][0]
    assert "--nosocket" not in runner.calls[-1][0]


def test_the_compile_environment_carries_no_secrets(monkeypatch):
    monkeypatch.setenv("UNIPILE_DSN", "secret")
    monkeypatch.setenv("TEXMFHOME", "/tex")

    env = latex.environment("/Library/TeX/texbin/lualatex")

    assert "UNIPILE_DSN" not in env and env["TEXMFHOME"] == "/tex"
    assert env["PATH"].startswith("/Library/TeX/texbin")


def test_characters_the_font_could_not_print_are_named():
    def runner(argv, cwd):
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        (cwd / "cv.log").write_text(
            "Missing character: There is no ≥ (U+2265) in font x!\n"
            "Missing character: There is no ✓ (U+2713) in font x!\n"
            "Missing character: There is no ≥ (U+2265) in font x!\n",
            encoding="utf-8",
        )
        return 0, "Output written on cv.pdf (1 page, 1 bytes)."

    assert latex.build(DOC, runner=runner).missing == ("≥", "✓")


@pytest.mark.skipif(shutil.which("lualatex") is None, reason="lualatex is not installed")
def test_a_real_build_reports_a_character_its_font_lacks():
    result = latex.build(DOC.replace("Hello", "Hello ✓"))

    assert result.ok and result.missing == ("✓",)
