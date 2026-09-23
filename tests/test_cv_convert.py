"""Turning an uploaded .tex into a template."""
from __future__ import annotations

import pytest
from conftest import FIXTURES, passing

from jobhunt.config import DEFAULT_CONFIG
from jobhunt.cv import convert, latex, templates, validate
from jobhunt.web import agent

GOOD = (FIXTURES / "cv" / "upload_template.tex").read_text(encoding="utf-8")
LEAKY = (FIXTURES / "cv" / "upload_leaky.tex").read_text(encoding="utf-8")
FINISHED = (FIXTURES / "cv" / "upload_finished.tex").read_text(encoding="utf-8")
GOOD_BODY = latex.split_preamble(GOOD)[1]
LEAKY_BODY = latex.split_preamble(LEAKY)[1]


@pytest.fixture
def cv_source(tmp_path, cfg):
    folder = tmp_path / "CV_Source"
    folder.mkdir()
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(folder / "master.tex")
    cfg.raw["tailoring"]["ats_check"] = str(tmp_path / "missing_ats.py")
    return folder


def scripted(*answers):
    replies = list(answers)
    prompts = []

    def agent_fn(prompt):
        prompts.append(prompt)
        return replies.pop(0)

    agent_fn.prompts = prompts
    return agent_fn


def desk(cfg, agent_fn=None):
    return convert.UploadDesk(cfg, agent=agent_fn or scripted(), runner=passing, background=False)


def test_template_is_a_model_phase():
    assert "template" in agent.PHASES and "template" in DEFAULT_CONFIG["models"]


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("cv.pdf", b"%PDF-1.7", "only .tex"),
        ("cv.tex", b"%PDF-1.7 renamed", "that is a PDF"),
        ("cv.tex", b"x" * 200_001, "200 KB"),
        ("cv.tex", b"\xff\xfe\x00", "UTF-8"),
        ("cv.tex", b"just some text", "\\documentclass"),
    ],
)
def test_an_unusable_upload_is_refused_in_words(name, data, reason):
    with pytest.raises(convert.UploadRefused) as caught:
        convert.read_upload(name, data)
    assert reason in str(caught.value)


def test_a_file_with_placeholders_is_already_a_template():
    assert convert.is_template(GOOD) and not convert.is_template(FINISHED)


def test_conversion_keeps_the_uploaded_preamble_byte_for_byte():
    sneaky = "\\documentclass{article}\n\\directlua{evil()}\n" + GOOD_BODY

    candidate, findings, rounds = convert.convert(
        FINISHED, scripted(sneaky), lambda c: validate.Findings([], [])
    )

    assert candidate.startswith(latex.split_preamble(FINISHED)[0])
    assert "evil" not in candidate and rounds == 1


def test_a_rejected_attempt_goes_back_with_the_problems(cfg, cv_source):
    agent_fn = scripted("```latex\n" + LEAKY_BODY + "```", GOOD_BODY)

    def check(candidate):
        return validate.validate(cfg, candidate, profile=templates.sample_profile(), runner=passing)

    candidate, findings, rounds = convert.convert(FINISHED, agent_fn, check)

    assert findings.ok and rounds == 2
    assert "was rejected" in agent_fn.prompts[1] and "prints hidden items" in agent_fn.prompts[1]


def test_an_answer_with_no_body_is_asked_for_again():
    agent_fn = scripted("I cannot do that.", GOOD_BODY)

    _, findings, rounds = convert.convert(FINISHED, agent_fn, lambda c: validate.Findings([], []))

    assert rounds == 2 and "- answer with the body only" in agent_fn.prompts[1]


def test_after_three_rejected_rounds_it_gives_up():
    agent_fn = scripted(LEAKY_BODY, LEAKY_BODY, LEAKY_BODY)

    _, findings, rounds = convert.convert(
        FINISHED, agent_fn, lambda c: validate.Findings(["it prints hidden items"], [])
    )

    assert not findings.ok and rounds == 3


def test_an_upload_that_is_already_a_template_is_only_checked(cfg, cv_source):
    uploading = desk(cfg, scripted())

    snapshot = uploading.start("mine.tex", GOOD.encode())

    assert snapshot["state"] == "done" and snapshot["mode"] == "template" and snapshot["acceptable"]
    assert snapshot["rounds"] == 0 and snapshot["suggested_name"] == "mine"


def test_a_finished_cv_is_converted_and_can_be_accepted(cfg, cv_source):
    uploading = desk(cfg, scripted(GOOD_BODY))
    uploading.start("Jordan Lee CV.tex", FINISHED.encode())

    added = uploading.accept("Jordan's look")

    assert added.name == "Jordan's look" and not added.trusted
    assert (added.folder / "original.tex").read_text(encoding="utf-8") == FINISHED
    assert added.text().startswith(latex.split_preamble(FINISHED)[0])
    assert uploading.snapshot()["state"] == "idle"


def test_a_template_that_failed_its_checks_cannot_be_accepted(cfg, cv_source):
    uploading = desk(cfg)
    snapshot = uploading.start("leaky.tex", LEAKY.encode())

    assert snapshot["state"] == "done" and not snapshot["acceptable"]
    with pytest.raises(convert.UploadFailed):
        uploading.accept("Leaky")


def test_a_second_upload_while_one_runs_is_refused(cfg, cv_source):
    uploading = convert.UploadDesk(cfg, agent=scripted(), runner=passing, background=True)
    uploading.state = "running"

    with pytest.raises(convert.UploadFailed):
        uploading.start("mine.tex", GOOD.encode())


def test_the_conversion_agent_gets_no_tools(cfg, cv_source, monkeypatch):
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout):
        seen.update(phase=phase, tools=tools)
        return GOOD_BODY

    monkeypatch.setattr(agent, "run", fake_run)
    convert.UploadDesk(cfg, runner=passing, background=False).start("cv.tex", FINISHED.encode())

    assert seen == {"phase": "template", "tools": ""}


def test_an_answer_that_mentions_the_document_markers_in_prose_still_yields_the_real_body():
    echo = "Here is the body, from \\begin{document} to \\end{document}:\n```latex\n" + GOOD_BODY + "```\n"
    candidate, _, _ = convert.convert(FINISHED, scripted(echo), lambda c: validate.Findings([], []))

    assert candidate.count("\\begin{document}") == 1 and "\\VAR{basics.name}" in candidate
    assert "Here is the body" not in candidate


def test_a_broken_profile_fails_the_upload_instead_of_hanging_it(cfg, cv_source):
    (cv_source / "profile.json").write_text("{ broken", encoding="utf-8")
    uploading = desk(cfg)

    snapshot = uploading.start("mine.tex", GOOD.encode())

    assert snapshot["state"] == "failed" and "profile.json" in snapshot["error"]
    uploading.discard()
    assert uploading.snapshot()["state"] == "idle"
