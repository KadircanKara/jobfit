"""The human-in-the-loop table. Nothing here is automatic.

PLAN.md non-negotiable 1: no auto-apply, ever. This module records that the user
applied, and hands off to the tailoring skill. It never submits a form, never
sends a message, and never contacts an employer.

The state machine, PLAN.md section 4:

    new -> surfaced -> interested -> applied -> screening -> interview -> offer
                                              -> rejected, ghosted
    skipped is terminal, and carries the reason.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunt.config import Config
from jobhunt.db.models import Application, Company, Job, Score, utcnow
from jobhunt.extract import ladder, quality
from jobhunt.integrations import tailoring

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("surfaced", "interested", "applied", "skipped"),
    "surfaced": ("interested", "applied", "skipped"),
    "interested": ("applied", "skipped"),
    "applied": ("screening", "interview", "rejected", "ghosted", "offer"),
    "screening": ("interview", "rejected", "ghosted", "offer"),
    "interview": ("offer", "rejected", "ghosted"),
    "offer": ("rejected",),
    "rejected": (),
    "ghosted": ("screening", "interview", "rejected"),
    "skipped": (),
}

MIN_QUALITY = 0.5

# Statuses that mean this job has already been dealt with. Re-applying to any of
# them is refused. Checking only for "applied" was a bug: a job advanced to
# "screening" could be applied to again, which silently reset it and lost the
# interview state.
ACTED_ON = frozenset({"applied", "screening", "interview", "offer", "rejected", "ghosted", "skipped"})


class ApplyBlocked(Exception):
    """Raised when applying would hand the tailoring skill something unusable."""


@dataclasses.dataclass
class ApplyResult:
    job_id: int
    folder: str
    files: list[str]
    instruction: str
    mode: str
    jd_source: str | None
    jd_quality: float | None
    extraction: ladder.Result | None = None

    def summary(self) -> str:
        quality = f" quality={self.jd_quality:.2f}" if self.jd_quality is not None else ""
        return (
            f"applied: job={self.job_id} folder={self.folder} "
            f"files={','.join(self.files)} jd={self.jd_source}{quality}"
        )


def apply(
    config: Config,
    session: Session,
    job_id: int,
    tailor: bool = True,
    dry_run: bool = False,
) -> ApplyResult:
    """Record an application and prepare the tailoring folder.

    Step 0 of the contract is the completeness check, and it is the reason this
    function can refuse. A CV tailored against a truncated JD is worse than no
    CV, because it looks finished.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ApplyBlocked(f"no job {job_id}")

    existing = _application_for(session, job)
    if existing is not None and existing.status in ACTED_ON:
        where = existing.folder_path or "no folder"
        raise ApplyBlocked(
            f"job {job_id} is already recorded as {existing.status} ({where}). "
            f"use `jobhunt status {job_id} <next>` to move it along"
        )

    extraction: ladder.Result | None = None
    if job.jd_completeness != "full":
        extraction = ladder.run(config, session, job)

    if job.jd_completeness != "full":
        raise ApplyBlocked(
            f"job {job_id} has no full JD (completeness={job.jd_completeness}). "
            f"run `jobhunt jd {job_id} --paste` and try again"
        )
    # An adapter marks a listing "full" without ever scoring it, so most jobs in
    # the corpus reach here with no quality score at all. Score it now rather
    # than treating the missing value as a failure: a job the source handed over
    # complete must not need a manual paste to get through.
    if job.jd_quality_score is None:
        job.jd_quality_score = quality.assess(job.description_text or job.description_md).score
    if job.jd_quality_score < MIN_QUALITY:
        raise ApplyBlocked(
            f"job {job_id} JD quality is {job.jd_quality_score:.2f}, below {MIN_QUALITY}. "
            f"run `jobhunt jd {job_id} --paste` and try again"
        )

    company = session.get(Company, job.company_id) if job.company_id else None
    score = session.scalars(
        select(Score).where(Score.job_id == job.id, Score.profile == job.market)
    ).first()

    handoff = tailoring.prepare(config, job, company, score, dry_run=dry_run)
    if handoff.folder.exists() and any(handoff.folder.iterdir()) and not dry_run:
        # prepare() created it, so "already had content" means a previous run.
        pre_existing = set(handoff.folder.iterdir()) - {
            handoff.folder / name for name in handoff.files
        }
        if pre_existing:
            raise ApplyBlocked(
                f"folder already exists with other files: {handoff.folder}. "
                "refusing to overwrite"
            )

    if not dry_run:
        row = existing or Application(job_id=job.id)
        row.status = "applied"
        row.applied_at = utcnow()
        row.folder_path = str(handoff.folder)
        row.last_status_change = utcnow()
        session.add(row)

    return ApplyResult(
        job_id=job.id,
        folder=str(handoff.folder),
        files=handoff.files,
        instruction=handoff.instruction if tailor else "",
        mode=handoff.mode,
        jd_source=job.jd_source,
        jd_quality=job.jd_quality_score,
        extraction=extraction,
    )


def skip(session: Session, job_id: int, reason: str) -> Application:
    """Terminal for this job. The reason is the training signal, so it is required."""
    job = session.get(Job, job_id)
    if job is None:
        raise ApplyBlocked(f"no job {job_id}")
    if not (reason or "").strip():
        raise ApplyBlocked("a skip needs a reason: it is what tunes the gate")

    row = _application_for(session, job) or Application(job_id=job.id)
    row.job_id = job.id
    row.status = "skipped"
    row.skip_reason = reason.strip()
    row.last_status_change = utcnow()
    session.add(row)
    return row


def advance(session: Session, job_id: int, status: str, note: str | None = None) -> Application:
    """Move an application along the state machine, refusing illegal jumps."""
    job = session.get(Job, job_id)
    if job is None:
        raise ApplyBlocked(f"no job {job_id}")
    row = _application_for(session, job)
    if row is None:
        raise ApplyBlocked(f"job {job_id} has no application row. `jobhunt apply {job_id}` first")

    current = row.status or "new"
    allowed = TRANSITIONS.get(current, ())
    if status not in allowed:
        raise ApplyBlocked(
            f"cannot go {current} -> {status}. allowed: {', '.join(allowed) or 'nothing, terminal'}"
        )
    row.status = status
    row.last_status_change = utcnow()
    if note:
        row.notes = f"{row.notes}\n{note}".strip() if row.notes else note
    return row


def ghost_stale(session: Session, after_days: int = 30) -> int:
    """Applied and silent for N days becomes ghosted. Bookkeeping, not a judgement."""
    cutoff = utcnow() - dt.timedelta(days=after_days)
    rows = session.scalars(
        select(Application).where(Application.status == "applied", Application.applied_at < cutoff)
    ).all()
    for row in rows:
        row.status = "ghosted"
        row.last_status_change = utcnow()
    return len(rows)


def _application_for(session: Session, job: Job) -> Application | None:
    """The application row for this job's whole cluster, not just this row.

    A job seen on three sources is one job. Applying through the Ashby row must
    stop the Himalayas row from resurfacing tomorrow.
    """
    cluster = job.canonical_job_id or job.id
    ids = [
        job_id
        for (job_id,) in session.execute(
            select(Job.id).where((Job.id == cluster) | (Job.canonical_job_id == cluster))
        ).all()
    ] or [job.id]
    return session.scalars(
        select(Application).where(Application.job_id.in_(ids)).order_by(Application.id)
    ).first()
