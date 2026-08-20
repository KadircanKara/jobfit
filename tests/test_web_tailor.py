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

    def __init__(self, *, verdicts=None, apply_fails=False):
        self.verdicts = verdicts or ["approve"]
        self.apply_fails = apply_fails
        self.prepared: list[int] = []
        self.tailored: list[str] = []
        self.reviewed = 0
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
