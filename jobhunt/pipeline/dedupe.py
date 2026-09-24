"""Two-layer deduplication. PLAN.md section 5.

Layer 1, exact identity: (source, external_id). Enforced by a unique constraint in
the schema and handled by the store on upsert, not here.

Layer 2, cross-source clustering: the same role legitimately appears on the company
board, on Himalayas, and on RemoteOK with three different titles. Group by company
plus normalized title, discriminate by country, and confirm with simhash distance
over the description.

Never dedupe on title alone. "Senior Backend Engineer" at two companies is two jobs,
so the company key is always part of the grouping key.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunt.db.models import Company, Job
from jobhunt.pipeline.simhash import DEFAULT_MAX_DISTANCE, from_hex, hamming

_CITY_TOKEN = re.compile(r"[a-z0-9]+")


@dataclasses.dataclass(frozen=True)
class DedupeRecord:
    """Everything clustering needs, and nothing else. Keeps the logic testable
    without a database."""

    id: int
    company_key: str
    title_normalized: str
    country: str | None
    description_hash: str | None
    first_seen_at: dt.datetime
    city: str | None = None
    seniority: str | None = None
    source: str = ""


def grouping_key(company_key: str, title_normalized: str) -> tuple[str, str]:
    """Country is deliberately not in the key.

    Aggregators frequently report no country for a job the company's own board
    places precisely. Putting country in the key would split those into two
    clusters, which is the exact failure clustering exists to prevent. It is used
    as a discriminator instead: two records with different known countries never
    merge, but a known country and an unknown one still can.
    """
    return (company_key, title_normalized)


def _conflict(left: str | None, right: str | None) -> bool:
    """Two known, different values mean two different jobs.

    One side unknown never blocks a merge: aggregators routinely drop the country,
    the city, or the seniority word from a posting the company board states fully.
    """
    return bool(left) and bool(right) and left != right


def _city_conflict(left: str | None, right: str | None) -> bool:
    """City names differ in form more than in substance across sources.

    "New York" and "New York City" are one place; "San Francisco" and "Seattle"
    are not. Token containment separates the two cases without a gazetteer.
    """
    if not left or not right:
        return False
    left_tokens = set(_CITY_TOKEN.findall(left.lower()))
    right_tokens = set(_CITY_TOKEN.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return False
    return not (left_tokens <= right_tokens or right_tokens <= left_tokens)


def _same_job(left: DedupeRecord, right: DedupeRecord, max_distance: int) -> bool:
    """Every discriminator must agree before two rows become one job.

    Location and seniority are discriminators rather than parts of the grouping
    key so that an unknown value still merges. Without the seniority check,
    "Manager" and "Senior Manager" at one company collapse into one card, because
    title_normalized strips the level by design.
    """
    if _conflict(left.country, right.country):
        return False
    if _city_conflict(left.city, right.city):
        return False
    if _conflict(left.seniority, right.seniority):
        return False
    return _descriptions_match(left.description_hash, right.description_hash, max_distance)


def _descriptions_match(left: str | None, right: str | None, max_distance: int) -> bool:
    """Missing hashes cannot disprove a match.

    Both records already agree on company and normalized title. With no description
    to compare, the grouping key is the best evidence available and clustering them
    is the lesser error: a false merge shows the alternate source on one card, a
    false split shows the user the same job three times.
    """
    if left is None or right is None:
        return True
    return hamming(from_hex(left), from_hex(right)) <= max_distance


def cluster(
    records: Iterable[DedupeRecord],
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> dict[int, int]:
    """Map every record id to its cluster head id.

    The head is the earliest-seen member, tie-broken by id so the result is stable
    across runs. A record that clusters with nothing maps to itself.
    """
    groups: dict[tuple[str, str], list[DedupeRecord]] = defaultdict(list)
    for record in records:
        groups[grouping_key(record.company_key, record.title_normalized)].append(record)

    canonical: dict[int, int] = {}
    for members in groups.values():
        members.sort(key=lambda r: (r.first_seen_at, r.id))
        heads: list[DedupeRecord] = []
        for record in members:
            for head in heads:
                if _same_job(head, record, max_distance):
                    canonical[record.id] = canonical[head.id]
                    break
            else:
                heads.append(record)
                canonical[record.id] = record.id
    return canonical


def records_from_db(session: Session, only_ids: list[int] | None = None) -> list[DedupeRecord]:
    """Load the clustering inputs.

    Scoped to the companies touched by the given jobs, because a new job can only
    ever join a cluster of jobs at the same company. Clustering the whole table on
    every sync would be quadratic in the corpus for no gain.
    """
    stmt = select(Job, Company).join(Company, Job.company_id == Company.id, isouter=True)
    if only_ids:
        company_ids = session.scalars(
            select(Job.company_id).where(Job.id.in_(only_ids), Job.company_id.is_not(None))
        ).all()
        if not company_ids:
            return []
        stmt = stmt.where(Job.company_id.in_(set(company_ids)))

    records = []
    for job, company in session.execute(stmt).all():
        if job.source == "upwork":
            # A freelance gig is never posted to a second board, so cross-source
            # clustering buys nothing here - and most Upwork clients never give
            # their name, so `normalize` falls back to one constant company
            # ("Upwork client") for all of them. Without this escape every
            # anonymous gig would share that one company key, and `grouping_key`
            # (company, normalized title) would collapse every "react developer"
            # gig from every client into one cluster. Escaping every Upwork row,
            # not just the anonymous ones, is deliberate: wrongly merging two
            # real gigs from the same client is worse than missing the rare case
            # of one client's job also being posted to an ATS board.
            key = f"__job{job.id}"
        else:
            key = (company.domain or company.normalized_name) if company else f"__job{job.id}"
        records.append(
            DedupeRecord(
                id=job.id,
                company_key=key,
                title_normalized=job.title_normalized,
                country=job.country,
                description_hash=job.description_hash,
                first_seen_at=job.first_seen_at,
                city=job.city,
                seniority=job.seniority,
                source=job.source,
            )
        )
    return records


def apply_clustering(session: Session, touched_ids: list[int] | None = None) -> int:
    """Recompute canonical_job_id for the affected slice. Returns rows changed."""
    records = records_from_db(session, touched_ids)
    if not records:
        return 0
    assignments = cluster(records)
    changed = 0
    jobs = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_(assignments))).all()}
    for job_id, head_id in assignments.items():
        job = jobs.get(job_id)
        if job is not None and job.canonical_job_id != head_id:
            job.canonical_job_id = head_id
            changed += 1
    return changed
