"""The tailoring skill's ATS check, as a template review reads it: every finding
with the words it names, and a check that did not complete never passes."""
from __future__ import annotations

from jobhunt.cv import ats


def test_the_ats_report_is_read_from_the_script_output(cfg, tmp_path):
    script = tmp_path / "ats_check.py"
    script.write_text("", encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(script)

    def runner(argv, cwd):
        assert (cwd / "cv.tex").exists() and (cwd / "cv.pdf").exists()
        return 1, (
            "[PASS] text layer\n"
            "[FAIL] text fidelity: 1 of 571 source words are missing:\n"
            "    band\n"
            "[PASS] no hidden text\n"
            "[WARN] links in text: x\n"
        )

    report = ats.check(cfg, "tex", b"%PDF", runner=runner)

    assert report.ran and report.failures == ["text fidelity: 1 of 571 source words are missing: band"]
    assert report.warnings == ["links in text: x"]


def test_without_the_script_the_ats_check_is_skipped_and_says_so(cfg, tmp_path):
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(tmp_path / "missing.py")

    report = ats.check(cfg, "tex", b"%PDF")

    assert not report.ran and "missing.py" in report.note


def test_a_timed_out_ats_check_is_not_a_clean_pass(cfg, tmp_path):
    script = tmp_path / "ats_check.py"
    script.write_text("", encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(script)

    report = ats.check(cfg, "tex", b"%PDF", runner=lambda argv, cwd: (ats.TIMED_OUT, "ran past 120 seconds"))

    assert not report.ran and "did not complete" in report.note


def test_a_crashed_ats_check_is_not_a_clean_pass(cfg, tmp_path):
    script = tmp_path / "ats_check.py"
    script.write_text("", encoding="utf-8")
    cfg.raw.setdefault("tailoring", {})["ats_check"] = str(script)

    report = ats.check(cfg, "tex", b"%PDF", runner=lambda argv, cwd: (1, "Traceback\nRuntimeError: bad pdf"))

    assert not report.ran and "bad pdf" in report.note
