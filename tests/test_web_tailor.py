"""Batch CV tailoring.

The reviewer gate is the point of this loop: a CV that the reviewer never
approved must never be marked ready, and a caught fabrication has to reach the
user even on a run that ends in approval.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from conftest import LatexRecorder, passing, save_profile
from fastapi.testclient import TestClient

from jobhunt.cv import templates
from jobhunt.web import tailor as tailor_module
from jobhunt.web.events import EventLog


class FakeSteps:
    """Stands in for `apply`, the tailoring agent, and the reviewer."""

    def __init__(
        self, *, verdicts=None, apply_fails=False, page_counts=None, verifier=None, delivers=True
    ):
        self.verdicts = verdicts or ["approve"]
        self.verifier = verifier or "verifier: ALL HARD CHECKS PASSED"
        self.delivers = delivers
        self.apply_fails = apply_fails
        # One entry per measurement, so a trim round can come back shorter.
        self.page_counts = list(page_counts or [2])
        self.prepared: list[int] = []
        self.templates: list[str | None] = []
        self.tailored: list[str] = []
        self.reviewed = 0
        self.measured = 0
        self.marked: list[tuple[int, str]] = []

    def prepare(self, job_id, template_id):
        if self.apply_fails:
            raise tailor_module.TailorError("job has no full JD")
        self.prepared.append(job_id)
        self.templates.append(template_id)
        return f"/folder/{job_id}"

    def tailor(self, folder, findings):
        self.tailored.append(folder)
        return self.verifier

    def delivered(self, folder):
        return self.delivers

    def review(self, folder, verifier):
        index = min(self.reviewed, len(self.verdicts) - 1)
        self.reviewed += 1
        verdict = self.verdicts[index]
        return {
            "verdict": verdict,
            "fit": {"score": 0.83},
            "fabrication": {
                "passed": verdict == "approve",
                "findings": [] if verdict == "approve" else ["“live SaaS product” — master says building"],
            },
        }

    def mark(self, job_id, status):
        self.marked.append((job_id, status))

    def pages(self, folder):
        index = min(self.measured, len(self.page_counts) - 1)
        self.measured += 1
        return self.page_counts[index]


def batch(steps, ids=(1,), rounds=3):
    return tailor_module.TailorBatch(
        job_ids=list(ids), steps=steps, log=EventLog(), max_rounds=rounds, concurrency=1
    )


def test_an_approved_cv_is_marked_ready():
    steps = FakeSteps(verdicts=["approve"])

    batch(steps).run()

    assert steps.marked == [(1, "cv_ready")]


def test_a_revise_verdict_sends_the_findings_back_and_tries_again():
    steps = FakeSteps(verdicts=["revise", "approve"])

    result = batch(steps).run()

    assert len(steps.tailored) == 2
    assert result[0].rounds == 2
    assert result[0].state == "approved"


def test_three_rounds_without_approval_fails_rather_than_shipping():
    steps = FakeSteps(verdicts=["revise", "revise", "revise"])

    result = batch(steps).run()

    assert result[0].state == "failed"
    assert steps.marked == [(1, "cv_failed")]


def test_a_failed_cv_reports_what_the_reviewer_kept_objecting_to():
    steps = FakeSteps(verdicts=["revise", "revise", "revise"])

    result = batch(steps).run()

    assert "live SaaS product" in " ".join(result[0].findings)


def test_fabrication_findings_survive_an_approved_run():
    """A caught fabrication is the most useful thing this loop produces."""
    steps = FakeSteps(verdicts=["revise", "approve"])

    result = batch(steps).run()

    assert result[0].findings, "the round-one finding must not be discarded on approval"


def test_a_job_without_a_complete_posting_is_not_tailored():
    steps = FakeSteps(apply_fails=True)

    result = batch(steps).run()

    assert result[0].state == "failed"
    assert steps.tailored == []
    assert steps.marked == []


def test_every_selected_job_is_processed():
    steps = FakeSteps(verdicts=["approve"])

    result = batch(steps, ids=(1, 2, 3)).run()

    assert [row.job_id for row in result] == [1, 2, 3]
    assert len(steps.prepared) == 3


def test_progress_reaches_the_event_log():
    steps = FakeSteps(verdicts=["approve"])
    run = batch(steps)

    run.run()

    phases = {event.phase for event in run.log}
    assert "tailor" in phases


def test_a_stop_request_leaves_the_rest_untouched():
    steps = FakeSteps(verdicts=["approve"])
    run = batch(steps, ids=(1, 2, 3))
    run.request_stop()

    result = run.run()

    assert steps.prepared == []
    assert all(row.state == "cancelled" for row in result)


# --- two pages ----------------------------------------------------------------


def test_a_two_page_cv_is_approved_as_it_stands():
    steps = FakeSteps(verdicts=["approve"], page_counts=[2])

    rows = batch(steps).run()

    assert rows[0].state == "approved"
    assert rows[0].pages == 2
    assert rows[0].rounds == 1


def test_an_over_long_cv_is_cut_again_rather_than_shipped():
    """Length is answered the same way a reviewer objection is: as a finding
    the next cut has to deal with."""
    steps = FakeSteps(verdicts=["approve"], page_counts=[3, 2])

    rows = batch(steps).run()

    assert rows[0].rounds == 2, "the long cut spends a round being trimmed"
    assert rows[0].pages == 2
    assert rows[0].state == "approved"
    assert any("3 pages" in finding for finding in rows[0].findings)


def test_a_cv_that_stays_long_still_ships_rather_than_failing():
    """The rounds are the budget. A CV the reviewer approved is not thrown away
    for being a page over."""
    steps = FakeSteps(verdicts=["approve"], page_counts=[3, 3, 3])

    rows = batch(steps, rounds=3).run()

    assert rows[0].state == "approved"
    assert rows[0].pages == 3
    assert ("cv_ready") in [status for _, status in steps.marked]


def test_a_toolchain_that_cannot_measure_does_not_hold_up_the_cv():
    class NoLatex(FakeSteps):
        def pages(self, folder):
            raise RuntimeError("no LaTeX toolchain found")

    steps = NoLatex(verdicts=["approve"])
    rows = batch(steps).run()

    assert rows[0].state == "approved"
    assert rows[0].pages is None


def test_preparing_a_folder_calls_apply_the_way_apply_is_defined(cfg, monkeypatch):
    """Regression: this passed (session, job_id) to a function whose signature
    is (config, session, job_id), so every browser-started tailoring run died
    on `apply() missing 1 required positional argument: 'job_id'`."""
    import inspect

    from jobhunt import applications
    from jobhunt.web import tailor as module

    seen = {}

    def fake_apply(config, session, job_id, tailor=True, dry_run=False, status="applied"):
        seen.update(config=config, job_id=job_id, tailor=tailor, status=status)
        return type("R", (), {"folder": f"/folder/{job_id}"})()

    # The fake must take the real parameters, in order, or it cannot catch this
    # class of bug. Names and order only: the annotations are not the point.
    assert list(inspect.signature(fake_apply).parameters) == list(
        inspect.signature(applications.apply).parameters
    )
    monkeypatch.setattr(applications, "apply", fake_apply)

    folder = module.ClaudeSteps(cfg).prepare(41, None)

    assert folder == "/folder/41"
    assert seen["job_id"] == 41
    assert seen["config"] is cfg
    assert seen["tailor"] is False
    assert seen["status"] == "tailored", "cutting a CV is not applying for anything"


# --- a template per job -------------------------------------------------------

def test_each_job_is_prepared_in_its_own_template():
    steps = FakeSteps()
    run = tailor_module.TailorBatch(
        job_ids=[1, 2], steps=steps, log=EventLog(), concurrency=1, picks={1: templates.get_builtin("modern")}
    )

    run.run()

    assert dict(zip(steps.prepared, steps.templates, strict=True)) == {1: "modern", 2: None}
    assert [(row.template_id, row.template_name) for row in run.rows] == [("modern", "Modern"), (None, "")]


class Applied:
    """`applications.apply`, making the folder the real one would."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root, self.calls = root, []

    def __call__(self, config, session, job_id, tailor=True, dry_run=False, status="applied"):
        self.calls.append(job_id)
        folder = self.root / f"Acme - Job {job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        return type("R", (), {"folder": str(folder)})()


@pytest.fixture
def applied(tmp_path, monkeypatch):
    from jobhunt import applications

    fake = Applied(tmp_path / "Tailored CVs")
    monkeypatch.setattr(applications, "apply", fake)
    return fake


def test_preparing_in_a_template_writes_that_master_into_the_folder(cfg, cv_source, applied):
    save_profile(cfg)

    folder = pathlib.Path(tailor_module.ClaudeSteps(cfg, runner=passing).prepare(41, "modern"))

    assert "Ada Lovelace" in (folder / "master.tex").read_text(encoding="utf-8")
    assert json.loads((folder / "master.json").read_text(encoding="utf-8"))["template_id"] == "modern"


def test_a_template_that_cannot_build_stops_before_any_folder_is_made(cfg, cv_source, applied):
    save_profile(cfg)

    def failing(argv, cwd):
        return 1, "! LaTeX Error: File `fontawesome5.sty' not found."

    with pytest.raises(tailor_module.TailorError, match="fontawesome5"):
        tailor_module.ClaudeSteps(cfg, runner=failing).prepare(41, "classic")
    assert applied.calls == []


def test_one_build_per_template_per_batch(cfg, cv_source, applied):
    save_profile(cfg)
    runner = LatexRecorder()

    steps = tailor_module.ClaudeSteps(cfg, runner=runner)
    for job_id in (1, 2, 3):
        steps.prepare(job_id, "classic")

    assert len(runner.calls) == 2, "one two-pass build, however many jobs share the template"


def test_without_a_template_the_folder_is_cut_from_the_global_master(cfg, cv_source, applied, monkeypatch):
    prompts = []
    monkeypatch.setattr(
        tailor_module.agent, "run", lambda config, phase, prompt, **kw: prompts.append(prompt) or "{}"
    )
    steps = tailor_module.ClaudeSteps(cfg)

    folder = steps.prepare(41, None)
    steps.tailor(folder, [])

    assert not (pathlib.Path(folder) / "master.tex").exists()
    assert f"Master CV: {cv_source / 'master.tex'}" in prompts[0]


def test_the_agents_are_pointed_at_the_folders_own_master(cfg, cv_source, applied, monkeypatch):
    save_profile(cfg)
    prompts = []
    answers = iter(["verifier output", '{"verdict": "approve"}'])
    def run(config, phase, prompt, **kw):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(tailor_module.agent, "run", run)
    steps = tailor_module.ClaudeSteps(cfg, runner=passing)

    folder = steps.prepare(41, "modern")
    steps.tailor(folder, [])
    steps.review(folder, "verifier output")

    snapshot = str(pathlib.Path(folder) / "master.tex")
    assert f"Master CV: {snapshot}" in prompts[0]
    assert "--master" in prompts[0] and "-m jobhunt.cv.tailored" in prompts[0]
    assert f"Master: {snapshot}" in prompts[1]


def test_the_page_count_comes_from_a_sandboxed_build(cfg, cv_source, tmp_path):
    folder = tmp_path / "Acme - Engineer"
    folder.mkdir()
    (folder / "cv.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    runner = LatexRecorder()

    assert tailor_module.ClaudeSteps(cfg, runner=runner).pages(str(folder)) == 2
    assert runner.calls and runner.calls[0][0].endswith("sandbox-exec")


# --- choosing templates for a batch -------------------------------------------


def test_with_no_profile_and_no_choice_nothing_is_chosen(cfg, cv_source):
    assert tailor_module.choose_templates(cfg, [1, 2], None) == {}


def test_a_choice_without_a_profile_is_refused(cfg, cv_source):
    with pytest.raises(tailor_module.TemplateChoice) as caught:
        tailor_module.choose_templates(cfg, [1], {"1": "modern"})

    assert caught.value.field == "profile"


def test_jobs_with_no_choice_take_the_default(cfg, cv_source):
    save_profile(cfg)

    chosen = tailor_module.choose_templates(cfg, [1, 2], {"2": "modern"})

    assert {job_id: template.id for job_id, template in chosen.items()} == {1: "classic", 2: "modern"}


@pytest.mark.parametrize(
    "requested",
    [{"1": "nope"}, {"1": 7}, {"9": "modern"}, {"one": "modern"}, ["modern"]],
)
def test_a_choice_that_does_not_fit_the_batch_is_refused(cfg, cv_source, requested):
    save_profile(cfg)

    with pytest.raises(tailor_module.TemplateChoice) as caught:
        tailor_module.choose_templates(cfg, [1], requested)

    assert caught.value.field == "templates"


@pytest.fixture
def client(cfg, cv_source, monkeypatch):
    from jobhunt.web.app import create_app

    monkeypatch.setattr(tailor_module.TailorBatch, "start", lambda self: None)
    return TestClient(create_app(config=cfg))


def test_starting_a_batch_names_each_jobs_template(client, cfg):
    save_profile(cfg)

    body = client.post("/api/tailor", json={"job_ids": [1, 2], "templates": {"2": "modern"}}).json()

    assert [(job["template_id"], job["template_name"]) for job in body["jobs"]] == [
        ("classic", "Classic"),
        ("modern", "Modern"),
    ]


def test_an_unknown_template_stops_the_batch_before_it_starts(client, cfg):
    save_profile(cfg)

    response = client.post("/api/tailor", json={"job_ids": [1], "templates": {"1": "nope"}})

    assert response.status_code == 422
    assert response.json()["field"] == "templates"
    assert client.get("/api/tailor").json()["jobs"] == []


def test_a_batch_with_no_profile_runs_as_it_always_has(client):
    body = client.post("/api/tailor", json={"job_ids": [1]}).json()

    assert body["started"] is True
    assert body["jobs"][0]["template_id"] is None


# --- a run that stops before the CV exists ------------------------------------


def test_a_run_that_stops_before_writing_the_cv_is_not_approved():
    steps = FakeSteps(
        verdicts=["approve"],
        delivers=False,
        verifier="[PASS] preamble frozen\n[FAIL] text fidelity: 1 of 321 source words are missing\n",
    )

    rows = batch(steps).run()

    assert rows[0].state == "failed"
    assert "text fidelity" in rows[0].error
    assert steps.reviewed == 0, "there is nothing for the reviewer to read"
    assert steps.marked == [(1, "cv_failed")]


def built_folder(cfg, tmp_path):
    """A folder whose cv.pdf the compile command built from the cv.tex in it."""
    from jobhunt.cv import tailored

    folder = tmp_path / "Acme - Engineer"
    folder.mkdir()
    (folder / "cv.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}\n")
    tailored.compile_here(cfg, folder / "cv.tex", runner=passing)
    return folder


def test_a_cv_is_delivered_when_the_named_pdf_is_the_last_build(cfg, cv_source, tmp_path):
    folder = built_folder(cfg, tmp_path)
    (folder / "Kadircan_Kara-CV.pdf").write_bytes((folder / "cv.pdf").read_bytes())

    assert tailor_module.ClaudeSteps(cfg).delivered(str(folder))


def test_a_named_pdf_from_an_earlier_round_is_not_a_delivery(cfg, cv_source, tmp_path):
    folder = built_folder(cfg, tmp_path)
    (folder / "Kadircan_Kara-CV.pdf").write_bytes(b"%PDF-1.7 round one")

    assert not tailor_module.ClaudeSteps(cfg).delivered(str(folder))


def test_a_cv_edited_after_its_last_build_is_not_a_delivery(cfg, cv_source, tmp_path):
    folder = built_folder(cfg, tmp_path)
    (folder / "Kadircan_Kara-CV.pdf").write_bytes((folder / "cv.pdf").read_bytes())
    (folder / "cv.tex").write_text("\\documentclass{article}\\begin{document}answered\\end{document}\n")

    assert not tailor_module.ClaudeSteps(cfg).delivered(str(folder))


def test_a_pdf_the_compile_command_did_not_build_is_not_a_delivery(cfg, cv_source, tmp_path):
    folder = built_folder(cfg, tmp_path)
    (folder / "cv.pdf").write_bytes(b"%PDF-1.7 from lualatex run by hand")
    (folder / "Kadircan_Kara-CV.pdf").write_bytes(b"%PDF-1.7 from lualatex run by hand")

    assert not tailor_module.ClaudeSteps(cfg).delivered(str(folder))


def test_no_named_pdf_is_no_delivery(cfg, cv_source, tmp_path):
    assert not tailor_module.ClaudeSteps(cfg).delivered(str(built_folder(cfg, tmp_path)))


def test_the_tailoring_agent_cannot_run_a_tex_engine_itself(cfg, cv_source, applied, monkeypatch):
    seen = {}

    def run(config, phase, prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "verifier output"

    monkeypatch.setattr(tailor_module.agent, "run", run)
    tailor_module.ClaudeSteps(cfg).tailor(str(applied.root), [])

    for engine in ("lualatex", "pdflatex", "xelatex", "latexmk"):
        assert f"Bash(*{engine}*)" in seen["disallowed"]
    assert "never run" in seen["prompt"].lower()
