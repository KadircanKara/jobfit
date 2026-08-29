"""SQLAlchemy models. Schema per PLAN.md sections 3.5 and 4.

SQLite is the permanent home, but the models stay ORM-portable so nothing here
depends on SQLite-specific SQL.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    # Lowercased, legal suffixes and punctuation stripped. Identity fallback when
    # no domain is known, which is the common case for ATS payloads.
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    domain: Mapped[str | None] = mapped_column(String(300), index=True)
    yc_batch: Mapped[str | None] = mapped_column(String(40))
    yc_slug: Mapped[str | None] = mapped_column(String(200))
    yc_tags: Mapped[list | None] = mapped_column(JSON)
    team_size: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(8))
    last_probed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    probe_status: Mapped[str | None] = mapped_column(String(20))  # resolved | unresolved | error

    jobs: Mapped[list[Job]] = relationship(back_populates="company")

    __table_args__ = (
        UniqueConstraint("normalized_name", "domain", name="uq_company_identity"),
    )

    @property
    def key(self) -> str:
        """The identity used for cross-source clustering.

        Domain when known, normalized name otherwise. PLAN.md section 5 says the
        company must be part of the dedupe key; most ATS payloads carry no domain,
        so the name is the working fallback rather than a reason to skip the check.
        """
        return self.domain or self.normalized_name


class Board(Base):
    """A fetchable ATS board. Discovery writes here, fetching reads from here."""

    __tablename__ = "boards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    token: Mapped[str] = mapped_column(String(200))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    # apply_url | yc | commoncrawl | crtsh | domain_probe | manual | fixture
    discovered_via: Mapped[str] = mapped_column(String(30))
    discovered_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate|validated|empty|dead
    tier: Mapped[str] = mapped_column(String(10), default="warm")  # hot | warm | cold
    market: Mapped[str] = mapped_column(String(20), default="global_remote")
    last_fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    next_fetch_at: Mapped[dt.datetime | None] = mapped_column(DateTime, index=True)
    last_job_count: Mapped[int | None] = mapped_column(Integer)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("provider", "token", name="uq_board_provider_token"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))
    market: Mapped[str] = mapped_column(String(20))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    # Which board produced this row. Not in PLAN.md section 4, added because
    # per-board deactivation (section 6) and tier promotion (section 3.5) both
    # need to know which board a job came from, and source alone is too coarse.
    board_id: Mapped[int | None] = mapped_column(ForeignKey("boards.id"), index=True)

    title: Mapped[str] = mapped_column(String(500))
    title_normalized: Mapped[str] = mapped_column(String(500), index=True)
    seniority: Mapped[str | None] = mapped_column(String(20))
    role_family: Mapped[str | None] = mapped_column(String(20))

    location_raw: Mapped[str | None] = mapped_column(String(500))
    country: Mapped[str | None] = mapped_column(String(8))
    city: Mapped[str | None] = mapped_column(String(200))
    remote_type: Mapped[str] = mapped_column(String(10), default="unknown")
    # full_time | part_time | contract | internship | temporary. Nullable, because
    # Greenhouse and RemoteOK do not state it and guessing would be worse.
    employment_type: Mapped[str | None] = mapped_column(String(20), index=True)
    tz_min_overlap_h: Mapped[float | None] = mapped_column(Float)

    salary_min: Mapped[float | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[float | None] = mapped_column(Numeric(14, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_period: Mapped[str | None] = mapped_column(String(10))
    salary_is_stated: Mapped[bool] = mapped_column(Boolean, default=False)

    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    description_md: Mapped[str | None] = mapped_column(Text)
    description_lang: Mapped[str | None] = mapped_column(String(8))
    description_en_md: Mapped[str | None] = mapped_column(Text)

    jd_completeness: Mapped[str] = mapped_column(String(10), default="none")  # full|snippet|none
    jd_source: Mapped[str | None] = mapped_column(String(20))
    jd_extracted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    jd_quality_score: Mapped[float | None] = mapped_column(Float)

    description_hash: Mapped[str | None] = mapped_column(String(32), index=True)  # simhash, hex
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    apply_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    canonical_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    missed_runs: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    repost_count: Mapped[int] = mapped_column(Integer, default=0)

    company: Mapped[Company | None] = relationship(back_populates="jobs")

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external"),
        Index("ix_job_market_active_seen", "market", "is_active", "first_seen_at"),
    )


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    profile: Mapped[str] = mapped_column(String(30))
    deterministic_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    deterministic_notes: Mapped[dict | None] = mapped_column(JSON)
    llm_score: Mapped[float | None] = mapped_column(Float)
    llm_reasoning: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(String(60))
    scored_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("job_id", "profile", name="uq_score_job_profile"),)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    folder_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    last_status_change: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Contact(Base):
    """A person, not a person-for-a-job.

    A recruiter who posts four roles is one row. Keeping identity separate from
    the attempt is what will later make "you messaged them on Tuesday"
    answerable across jobs, which a per-job contact row could never do.
    """

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    headline: Mapped[str | None] = mapped_column(String(500))
    # Unique when present. A contact entered by hand from two different jobs is
    # the same person and must collapse to one row; a contact with only a name
    # is allowed and simply does not dedupe.
    profile_url: Mapped[str | None] = mapped_column(Text, unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(120))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # job_poster | manual
    # Null means never checked. Treated as "not connected" by the routing tree,
    # which is the branch that offers a choice rather than assuming a free send.
    is_connection: Mapped[bool | None] = mapped_column(Boolean)
    can_send_inmail: Mapped[bool | None] = mapped_column(Boolean)
    status_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Outreach(Base):
    """One attempt to reach one person about one job. The unit the user approves.

    Deliberately not part of the application state machine: messaging a recruiter
    is not applying, and folding the two together would put jobs into `applied`
    that were never sent.
    """

    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    route: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="none")
    body: Mapped[str | None] = mapped_column(Text)
    # Reserved for the 300-character invite note when it differs from the DM
    # body. Splitting it later would be a migration; dropping it is not.
    note_body: Mapped[str | None] = mapped_column(Text)
    drafted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    invited_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    failure: Mapped[str | None] = mapped_column(Text)
    last_state_change: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("job_id", "contact_id", name="uq_outreach_job_contact"),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Not unique: re-normalizing a stored run with --from-raw is a legitimate
    # second run over the same payloads, and it must not collide with the fetch
    # that produced them.
    run_key: Mapped[str] = mapped_column(String(60), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(40))
    market: Mapped[str | None] = mapped_column(String(20))
    raw_fetched: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | degraded | failed
    error_detail: Mapped[str | None] = mapped_column(Text)


class Meta(Base):
    """Tiny key-value store for pipeline watermarks and cache timestamps.

    Harvesting, the Common Crawl backfill, and the crt.sh cache all need to
    remember where they stopped. A table beats a stray JSON file next to the
    database, because it moves with the database and rolls back with it.
    """

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
