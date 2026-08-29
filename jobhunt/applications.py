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
import pathlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobhunt.config import Config
from jobhunt.db.models import Application, Company, Job, Score, utcnow
from jobhunt.extract import ladder, quality
from jobhunt.integrations import tailoring

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("surfaced", "interested", "tailored", "applied", "skipped"),
    "surfaced": ("interested", "tailored", "applied", "skipped"),
    "interested": ("tailored", "applied", "skipped"),
    # A tailored CV is a folder on disk, not a submission. It goes on to
    # applied when you actually send it, or to skipped when you decide not to.
    "tailored": ("applied", "skipped", "interested"),
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
# Statuses that mean a folder already exists, so preparing one again would
# overwrite work. `tailored` belongs here: the CV was cut.
ACTED_ON = frozenset({
    "tailored", "applied", "screening", "interview", "offer", "rejected", "ghosted", "skipped",
})

# Statuses that take a job off the shortlist for good. Deliberately *not* the
# same set: cutting a CV is not applying for anything, and a job you have
# tailored for but not sent is exactly the one you still need to see.
SETTLED = frozenset({
    "applied", "screening", "interview", "offer", "rejected", "ghosted", "skipped",
})


# The tailoring skill ships the CV as "<Name>-CV.pdf" beside cv.tex; older
# folders spell the separator with an underscore. Either one is proof the CV was
# actually compiled, which a folder on its own is not: a run that died after
# prepare() leaves jd.txt and nothing else, and refusing that job forever means
# it can never be tailored again.
CV_PDF_GLOBS = ("*-CV.pdf", "*_CV.pdf")


def tailored_cv(folder: pathlib.Path | str | None) -> pathlib.Path | None:
    """The compiled CV in an application folder, or None if there is not one."""
    if not folder:
        return None
    path = pathlib.Path(folder)
    if not path.is_dir():
        return None
    for pattern in CV_PDF_GLOBS:
        for found in sorted(path.glob(pattern)):
            # A zero-byte file is a failed compile, not a CV.
            if found.is_file() and found.stat().st_size > 0:
                return found
    return None


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
    status: str = "applied",
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
        # "tailored" only claims a folder was prepared. If the CV never came out
        # of it the job is not done, so let this run finish what the last one
        # started. Every other acted-on status is a real decision and still refuses.
        resuming = existing.status == "tailored" and tailored_cv(existing.folder_path) is None
        if not resuming:
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
    if not dry_run:
        # What is worth protecting is the compiled CV, not the folder. Leftovers
        # from a run that never produced one (jd.txt, a half-written cv.tex) are
        # what this run is here to replace.
        cv = tailored_cv(handoff.folder)
        if cv is not None:
            raise ApplyBlocked(
                f"folder already has a tailored CV: {cv}. refusing to overwrite"
            )

    if not dry_run:
        row = existing or Application(job_id=job.id)
        row.status = status
        # Only a real application has an applied_at. Stamping one on a tailored
        # folder would make `ghost_stale` treat an unsent CV as a silent
        # employer thirty days later.
        if status == "applied":
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


@dataclasses.dataclass
class Rename:
    """One folder the naming scheme would move, or could not place."""

    old: str
    new: str = ""
    reason: str = ""

    @property
    def doable(self) -> bool:
        return bool(self.new) and not self.reason


def plan_renames(config: Config, session: Session) -> list[Rename]:
    """What the current naming scheme would call every existing folder.

    A folder is placed by its recorded application first, and by reading
    "<Company> - <Position>" off its own name second, because most folders here
    were made by running the skill by hand and have no row at all.
    """
    root = pathlib.Path(str(config.get("tailoring", "applications_root"))).expanduser()
    if not root.is_dir():
        return []

    by_path = {
        str(row.folder_path): row
        for row in session.scalars(select(Application).where(Application.folder_path.is_not(None)))
    }

    plans: list[Rename] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        where, reason = _place_for(session, folder, by_path)
        if reason:
            plans.append(Rename(old=str(folder), reason=reason))
            continue
        # Only the location is added. The company and title already on disk are
        # what the skill or the user wrote, and they are better than anything
        # re-derived: a board that calls itself "apify" would otherwise
        # overwrite a folder correctly named "Apify".
        base = folder.name.split(" @ ", 1)[0].rstrip()
        target = folder.parent / (f"{base} @ {where}" if where else base)
        if target == folder:
            continue
        if target.exists():
            plans.append(Rename(old=str(folder), reason=f"{target.name} already exists"))
            continue
        plans.append(Rename(old=str(folder), new=str(target)))
    return plans


def _place_for(
    session: Session, folder: pathlib.Path, by_path: dict[str, Application]
) -> tuple[str, str]:
    """(location, reason it could not be found). Exactly one is ever set."""
    recorded = by_path.get(str(folder))
    if recorded is not None:
        job = session.get(Job, recorded.job_id)
        if job is not None:
            return tailoring.place(job.location_raw), ""

    # No row, so read the name the skill wrote. Anything after " @ " is a
    # location this scheme added, and is not part of the title.
    name = folder.name.split(" @ ", 1)[0]
    if " - " not in name:
        return "", "the name is not '<Company> - <Position>'"
    company_name, _, title = name.partition(" - ")
    matches = session.scalars(
        select(Job)
        .join(Company, Job.company_id == Company.id, isouter=True)
        .where(Job.title == title.strip())
        .where(func.lower(Company.name) == company_name.strip().lower())
    ).all()
    if not matches:
        return "", "no posting in the corpus matches it"

    places = {tailoring.place(job.location_raw) for job in matches}
    places.discard("")
    if len(places) > 1:
        # One company advertising the same title in several cities. Guessing
        # would put the wrong city on a real application folder.
        return "", f"{len(matches)} postings match and their locations differ"
    return (places.pop() if places else ""), ""


def rename_folders(config: Config, session: Session, dry_run: bool = True) -> list[Rename]:
    """Move every folder onto the current naming scheme.

    The rename and the recorded path move together: a folder renamed without
    its `folder_path` is a CV the studio can no longer find.
    """
    plans = plan_renames(config, session)
    if dry_run:
        return plans

    for plan in plans:
        if not plan.doable:
            continue
        source, target = pathlib.Path(plan.old), pathlib.Path(plan.new)
        source.rename(target)
        for row in session.scalars(
            select(Application).where(Application.folder_path == plan.old)
        ):
            row.folder_path = plan.new
    return plans


def record_applied(session: Session, job_id: int, folder: str | None = None) -> Application:
    """Record an application without preparing a tailoring folder.

    Used by `csv mark-applied`, where the folder already exists (the tailoring
    run made it) or is not wanted. Same cluster semantics as apply: the job stops
    resurfacing on every board it appears on.
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ApplyBlocked(f"no job {job_id}")
    row = _application_for(session, job)
    # SETTLED, not ACTED_ON. ACTED_ON also holds `tailored`, and a tailored job
    # is the one case this has to let through: the CV is cut, the application is
    # not sent, and recording that it now has been is the whole point. Guarding
    # on ACTED_ON made this a silent no-op for exactly those jobs while
    # `csv mark-applied` flipped the column, so the file said applied and the
    # shortlist kept offering the job. SETTLED still protects a job that has
    # moved past applying - screening, interview, offer - from being dragged back.
    if row is not None and row.status in SETTLED:
        return row
    row = row or Application(job_id=job.id)
    row.job_id = row.job_id or job.id
    row.status = "applied"
    row.applied_at = row.applied_at or utcnow()
    row.last_status_change = utcnow()
    if folder:
        row.folder_path = folder
    session.add(row)
    return row
