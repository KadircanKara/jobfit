"""Recording, from the browser, that an application was actually sent.

Cutting a CV is not applying. The shortlist deliberately keeps a `tailored`
job visible (applications.SETTLED excludes it) precisely because a prepared
application that never went out is the one still worth seeing. That only works
if there is a way to say "sent" - and until now the only one was a CLI command.

Reversible on purpose. This is the single action that removes a job from the
shortlist for good, so a mis-click has to cost nothing.
"""
from __future__ import annotations

from sqlalchemy import select

from jobhunt import applications
from jobhunt.config import Config
from jobhunt.db.models import Application, Job
from jobhunt.db.session import session_scope
from jobhunt.render import csv_export


class UnknownJob(Exception):
    """No job with that id. Surfaced as a refusal, never as a crash."""

    def __init__(self, job_id: int) -> None:
        super().__init__(str(job_id))
        self.job_id = job_id


def set_applied(config: Config, job_id: int, applied: bool) -> bool:
    """Mark the job applied, or put it back. Returns the state that now holds."""
    with session_scope(config.db_path) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise UnknownJob(job_id)
        if applied:
            applications.record_applied(session, job_id)
        else:
            _undo(session, job)

    path = csv_export.csv_path(config)
    csv_export.set_applied(path, job_id, applied)
    return applied


def _undo(session, job: Job) -> None:
    """Take the send back, without throwing away the work that preceded it.

    A folder means the CV was cut, so the row returns to `tailored` and the job
    reappears on the shortlist with its folder intact. With no folder the row
    only ever existed to record the send, so it goes entirely rather than
    leaving a job sitting in a status it was never really in.
    """
    row = session.scalars(
        select(Application).where(Application.job_id == job.id)
    ).first()
    if row is None:
        return
    if row.folder_path:
        row.status = "tailored"
        row.applied_at = None
    else:
        session.delete(row)


def applied_job_ids(config: Config) -> list[int]:
    """Every job recorded as sent. Small by nature - one row per application."""
    with session_scope(config.db_path) as session:
        rows = session.execute(
            select(Application.job_id).where(Application.status == "applied")
        ).all()
    return sorted(job_id for (job_id,) in rows)
