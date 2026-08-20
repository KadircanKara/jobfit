"""Database writes. The only module that turns a JobPosting into a row.

Layer 1 dedupe lives here: (source, external_id) is the exact identity, so a
posting already seen from the same source refreshes the existing row and never
creates a second one.
"""
from __future__ import annotations

import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunt.db.models import Board, Company, Job, Meta, utcnow
from jobhunt.pipeline import normalize as norm
from jobhunt.pipeline import simhash
from jobhunt.sources.base import JobPosting


@dataclasses.dataclass
class UpsertResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    reposts: int = 0
    touched_ids: list[int] = dataclasses.field(default_factory=list)

    @property
    def total(self) -> int:
        return self.new + self.updated + self.unchanged


def get_or_create_company(session: Session, name: str, domain: str | None) -> Company:
    """Resolve a company by domain first, then by normalized name.

    Domain is the real identity key, but most ATS payloads carry none, so the
    normalized name carries identity until a domain shows up and upgrades the row.
    """
    normalized = norm.normalize_company_name(name)
    company: Company | None = None
    if domain:
        company = session.scalars(select(Company).where(Company.domain == domain)).first()
    if company is None and normalized:
        company = session.scalars(
            select(Company).where(Company.normalized_name == normalized)
        ).first()
    if company is None:
        company = Company(name=name.strip(), normalized_name=normalized, domain=domain)
        session.add(company)
        session.flush()
    elif domain and not company.domain:
        company.domain = domain
    return company


def get_or_create_board(
    session: Session, provider: str, token: str, discovered_via: str, market: str
) -> Board:
    board = session.scalars(
        select(Board).where(Board.provider == provider, Board.token == token)
    ).first()
    if board is None:
        board = Board(
            provider=provider, token=token, discovered_via=discovered_via, market=market
        )
        session.add(board)
        session.flush()
    return board


def upsert_posting(
    session: Session, posting: JobPosting, board: Board | None = None
) -> tuple[Job, str]:
    """Insert or refresh one posting. Returns (row, 'new' | 'updated' | 'unchanged')."""
    company = get_or_create_company(session, posting.company_name, posting.company_domain)
    now = utcnow()

    content_hash = norm.content_hash(
        posting.title, posting.location_raw, posting.description_text
    )
    description_hash = (
        simhash.to_hex(simhash.simhash(posting.description_text))
        if posting.description_text
        else None
    )

    existing = session.scalars(
        select(Job).where(Job.source == posting.source, Job.external_id == posting.external_id)
    ).first()

    fields = _row_fields(posting, company, board, content_hash, description_hash)

    if existing is None:
        job = Job(**fields, first_seen_at=now, last_seen_at=now)
        session.add(job)
        session.flush()
        return job, "new"

    outcome = "unchanged" if existing.content_hash == content_hash else "updated"
    # A job that went inactive and came back under the same external_id is a repost.
    if not existing.is_active:
        existing.repost_count += 1
    for key, value in fields.items():
        setattr(existing, key, value)
    existing.last_seen_at = now
    existing.missed_runs = 0
    existing.is_active = True
    return existing, outcome


def _row_fields(
    posting: JobPosting,
    company: Company,
    board: Board | None,
    content_hash: str,
    description_hash: str | None,
) -> dict:
    return {
        "external_id": posting.external_id,
        "source": posting.source,
        "market": posting.market,
        "company_id": company.id,
        "board_id": board.id if board else None,
        "title": posting.title,
        "title_normalized": norm.normalize_title(posting.title),
        "seniority": norm.detect_seniority(posting.title, posting.description_text),
        "role_family": norm.detect_role_family(posting.title),
        "location_raw": posting.location_raw,
        "country": posting.country,
        "city": posting.city,
        "remote_type": posting.remote_type,
        "employment_type": posting.employment_type,
        "salary_min": posting.salary_min,
        "salary_max": posting.salary_max,
        "salary_currency": posting.salary_currency,
        "salary_period": posting.salary_period,
        "salary_is_stated": posting.salary_is_stated,
        "description_html": posting.description_html,
        "description_text": posting.description_text,
        "description_md": posting.description_md,
        "description_lang": posting.description_lang,
        "jd_completeness": posting.jd_completeness,
        "jd_source": posting.jd_source,
        "jd_extracted_at": utcnow() if posting.jd_completeness == "full" else None,
        "description_hash": description_hash,
        "content_hash": content_hash,
        "posted_at": posting.posted_at,
        "apply_url": posting.apply_url,
        "source_url": posting.source_url,
    }


def deactivate_missing(
    session: Session, board: Board, seen_external_ids: set[str], grace_runs: int = 2
) -> int:
    """Mark jobs that stopped appearing in a board's full listing.

    PLAN.md section 6: two consecutive successful runs, not one. A single flaky
    listing must never wipe a board's jobs. Rows are never deleted.
    """
    stale = session.scalars(
        select(Job).where(Job.board_id == board.id, Job.is_active.is_(True))
    ).all()
    deactivated = 0
    for job in stale:
        if job.external_id in seen_external_ids:
            continue
        job.missed_runs += 1
        if job.missed_runs >= grace_runs:
            job.is_active = False
            deactivated += 1
    return deactivated


def meta_get(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(Meta, key)
    return row.value if row else default


def meta_set(session: Session, key: str, value: str) -> None:
    row = session.get(Meta, key)
    if row is None:
        session.add(Meta(key=key, value=value))
    else:
        row.value = value
