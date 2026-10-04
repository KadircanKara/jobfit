"""Shortlist rendering: the interactive card and the plain-text digest.

Two output modes over one query, deliberately. The digest is what a cron line
pipes into a file the user reads over coffee; it makes no LLM call, asks no
questions, and exits 0. The interactive card is the same data with keys attached.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import and_, or_, select

from jobhunt import applications
from jobhunt.config import Config
from jobhunt.db.models import Application, Company, Job, Score, utcnow
from jobhunt.db.session import session_scope
from jobhunt.rank import deterministic


@dataclasses.dataclass
class Card:
    job_id: int
    title: str
    company: str
    market: str
    score: float | None
    boost: float
    location: str
    remote_type: str
    employment_type: str | None
    salary: str | None
    posted_at: dt.datetime | None
    reasoning: str | None
    apply_url: str | None
    source: str
    also_on: list[str]
    jd_completeness: str
    yc_batch: str | None
    team_size: int | None

    @property
    def rank_key(self) -> float:
        """Gate score times the deterministic boost.

        A job with no gate score sorts below every scored one rather than at the
        top, which is what a naive None-to-zero-or-one would do.
        """
        return (self.score or 0.0) * self.boost

    def age_text(self, now: dt.datetime | None = None) -> str:
        if not self.posted_at:
            return "posted ?"
        days = max(0, ((now or utcnow()) - self.posted_at).days)
        return "posted today" if days == 0 else f"posted {days}d ago"


# A job is not shown once its listing has stopped carrying it, whatever its
# `is_active` says (that waits for two misses). For a job board that is the
# board's own latest fetch: a job missing from it is gone, and a board nobody
# has fetched lately says nothing either way, so wall-clock age must not hide
# its jobs. A search (LinkedIn, Workable's job search, kariyer.net, Careerjet)
# returns only its top results and proves nothing by leaving a job out, so
# there - and for a job with no board - a job not seen for this many days is
# not shown.
SEEN_WITHIN_DAYS = 14
SEARCH_SOURCES = ("linkedin", "workable_search", "kariyer_net", "careerjet")


def shortlist(
    config: Config,
    market: str | None = None,
    limit: int = 15,
    since_days: int | None = None,
    min_score: float | None = None,
    include_unscored: bool = False,
) -> list[Card]:
    """Jobs that passed stage 1, cleared the market's surface threshold, and
    have not been acted on."""
    cards, _ = _scored_cards(
        config,
        market=market,
        since_days=since_days,
        min_score=min_score,
        include_unscored=include_unscored,
    )
    return cards[:limit]


def near_misses(
    config: Config,
    market: str | None = None,
    limit: int = 6,
    since_days: int | None = None,
    min_score: float | None = None,
) -> list[Card]:
    """The best jobs that the surface threshold turned away.

    Kept separate from `shortlist` rather than returned alongside it, so a job
    below the bar can never reach the CSV or a digest by accident. It exists
    because a run returning three jobs usually means the bar moved, not the
    market, and that is invisible when the rejected rows are simply gone.
    """
    _, below = _scored_cards(
        config, market=market, since_days=since_days, min_score=min_score
    )
    return below[:limit]


def _scored_cards(
    config: Config,
    market: str | None = None,
    since_days: int | None = None,
    min_score: float | None = None,
    include_unscored: bool = False,
) -> tuple[list[Card], list[Card]]:
    """Every eligible card, split into those above the bar and those below."""
    filters = deterministic.load_filters(config)
    cards: list[Card] = []
    below: list[Card] = []

    with session_scope(config.db_path) as session:
        # A job the user already applied to or skipped never surfaces again, and
        # that has to hold across every source it appears on, so the whole
        # cluster is excluded, not just the row that was acted on.
        acted_on = _acted_on_clusters(session)

        stmt = (
            select(Job, Company, Score)
            # The row for the job's current market. A job that moved market keeps
            # its old row, and reading that one let a stale pass or score through.
            .join(Score, and_(Score.job_id == Job.id, Score.profile == Job.market))
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.is_active.is_(True))
            .where(
                or_(
                    and_(
                        Job.board_id.is_not(None), Job.source.not_in(SEARCH_SOURCES), Job.missed_runs == 0
                    ),
                    and_(
                        or_(Job.board_id.is_(None), Job.source.in_(SEARCH_SOURCES)),
                        Job.last_seen_at >= utcnow() - dt.timedelta(days=SEEN_WITHIN_DAYS),
                    ),
                )
            )
            .where(Score.deterministic_pass.is_(True))
        )
        if market:
            stmt = stmt.where(Job.market == market)
        if since_days:
            stmt = stmt.where(Job.first_seen_at >= utcnow() - dt.timedelta(days=since_days))
        if not include_unscored:
            stmt = stmt.where(Score.llm_score.is_not(None))

        for job, company, score in session.execute(stmt).all():
            cluster = job.canonical_job_id or job.id
            if cluster in acted_on:
                continue
            threshold = min_score if min_score is not None else _threshold(filters, job.market)
            card = _card(session, job, company, score)
            if score.llm_score is not None and threshold is not None and score.llm_score < threshold:
                below.append(card)
                continue
            cards.append(card)

    cards.sort(key=_rank_order, reverse=True)
    below.sort(key=_rank_order, reverse=True)
    return cards, below


def _rank_order(card: Card) -> tuple[float, int]:
    return card.rank_key, card.job_id


def _threshold(filters: dict, market: str) -> float | None:
    value = deterministic.profile_for(filters, market).get("min_score_to_surface")
    return float(value) if value is not None else None


def _acted_on_clusters(session) -> set[int]:
    clusters: set[int] = set()
    rows = session.execute(
        select(Job.id, Job.canonical_job_id)
        .join(Application, Application.job_id == Job.id)
        .where(Application.status.in_(applications.SETTLED))
    ).all()
    for job_id, canonical in rows:
        clusters.add(canonical or job_id)
    return clusters


def _card(session, job: Job, company: Company | None, score: Score) -> Card:
    also_on: list[str] = []
    if job.canonical_job_id:
        also_on = [
            other
            for (other,) in session.execute(
                select(Job.source)
                .where(Job.canonical_job_id == job.canonical_job_id, Job.id != job.id)
            ).all()
        ]
    notes = score.deterministic_notes or {}
    return Card(
        job_id=job.id,
        title=job.title,
        company=company.name if company else "?",
        market=job.market,
        score=score.llm_score,
        boost=float(notes.get("boost") or 1.0),
        location=job.location_raw or job.city or job.country or "?",
        remote_type=job.remote_type,
        employment_type=job.employment_type,
        salary=_salary(job),
        posted_at=job.posted_at,
        reasoning=score.llm_reasoning,
        apply_url=job.apply_url,
        source=job.source,
        also_on=sorted(set(also_on)),
        jd_completeness=job.jd_completeness,
        yc_batch=company.yc_batch if company else None,
        team_size=company.team_size if company else None,
    )


def _salary(job: Job) -> str | None:
    if not job.salary_is_stated or job.salary_min is None:
        return None
    high = int(job.salary_max or job.salary_min)
    return f"{int(job.salary_min):,}-{high:,} {job.salary_currency or ''}".strip()


def render_card(card: Card, now: dt.datetime | None = None) -> str:
    """The compact card from PLAN.md section 9, as plain text."""
    score = f"score {card.score:.2f}" if card.score is not None else "ungated"
    header = f"[{card.job_id}] {card.title}"
    second = " · ".join(
        part for part in (
            card.company,
            card.yc_batch,
            f"{card.team_size} people" if card.team_size else None,
            f"{card.location} ({card.remote_type})",
        ) if part
    )
    third = " · ".join(
        part for part in (
            card.salary,
            card.age_text(now),
            f"also on: {', '.join(card.also_on)}" if card.also_on else None,
            None if card.jd_completeness == "full" else f"jd: {card.jd_completeness}",
        ) if part
    )
    lines = [f"{header:<62}{score:>12}", f"      {second}"]
    if third:
        lines.append(f"      {third}")
    if card.reasoning:
        lines.append(f'      "{card.reasoning}"')
    if card.apply_url:
        lines.append(f"      {card.apply_url}")
    return "\n".join(lines)


def render_digest(cards: list[Card], now: dt.datetime | None = None) -> str:
    """Plain text for piping into a file or an email. No colour, no prompts."""
    if not cards:
        return "jobhunt digest: nothing new above threshold."
    stamp = (now or utcnow()).strftime("%Y-%m-%d")
    out = [f"jobhunt digest {stamp} - {len(cards)} job(s)", ""]
    for card in cards:
        out.append(render_card(card, now))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
