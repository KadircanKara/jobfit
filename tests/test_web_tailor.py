"""Batch CV tailoring.

The reviewer gate is the point of this loop: a CV that the reviewer never
approved must never be marked ready, and a caught fabrication has to reach the
user even on a run that ends in approval.
"""
from __future__ import annotations

from jobhunt.web import tailor as tailor_module
from jobhunt.web.events import EventLog


class FakeSteps:
    """Stands in for `apply`, the tailoring agent, and the reviewer."""

    def __init__(self, *, verdicts=None, apply_fails=False, page_counts=None):
        self.verdicts = verdicts or ["approve"]
        self.apply_fails = apply_fails
        # One entry per measurement, so a trim round can come back shorter.
        self.page_counts = list(page_counts or [2])
        self.prepared: list[int] = []
        self.tailored: list[str] = []
        self.reviewed = 0
        self.measured = 0
        self.marked: list[tuple[int, str]] = []

    def prepare(self, job_id):
        if self.apply_fails:
            raise tailor_module.TailorError("job has no full JD")
        self.prepared.append(job_id)
        return f"/folder/{job_id}"

    def tailor(self, folder, findings):
        self.tailored.append(folder)
        return "verifier: ALL HARD CHECKS PASSED"

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

    folder = module.ClaudeSteps(cfg).prepare(41)

    assert folder == "/folder/41"
    assert seen["job_id"] == 41
    assert seen["config"] is cfg
    assert seen["tailor"] is False
    assert seen["status"] == "tailored", "cutting a CV is not applying for anything"
