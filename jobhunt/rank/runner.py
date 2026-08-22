"""Ranking orchestration: stage 1 in process, stage 2 over a file.

There is no API key in this setup by design, so the LLM gate does not run
inline. Instead:

    jobhunt rank                       stage 1, deterministic, writes scores
    jobhunt rank --emit batch.json     writes the survivors plus the prompt
    <the gate runs in Claude Code, reading that file>
    jobhunt rank --ingest verdicts.json  writes the scores back

The alternative, calling out to a model from inside the CLI, would need a key
this setup does not have, and would make `jobhunt rank` un-cronable anyway. The
file protocol keeps cron doing pure `sync`, keeps the expensive step explicitly
human-triggered, and makes every gate run inspectable and replayable after the
fact, which an inline call would not be.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from jobhunt.config import Config
from jobhunt.db.models import Company, Job, Score, utcnow
from jobhunt.db.session import session_scope
from jobhunt.rank import deterministic, profile

# The gate reads a truncated description. PLAN.md section 7 says roughly 1500
# tokens; 6000 characters is that, and it keeps a 20 job batch inside a sane
# context without the caller having to think about it.
DESCRIPTION_CHARS = 6000
DEFAULT_BATCH = 20
# One company posting 40 near-identical roles would otherwise fill an entire
# batch, which wastes the gate on variations of one decision. Seen live: 11 of
# a 12 job batch were one translation agency's freelance listings.
MAX_PER_COMPANY = 3


# How often a long pass reports in. Small enough that the browser sees the
# counts climb, large enough that the callback is not the expensive part.
PROGRESS_EVERY = 50


@dataclasses.dataclass
class DeterministicResult:
    scored: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    corpus: int = 0
    by_market: dict[str, int] = dataclasses.field(default_factory=dict)
    # Drop reason code -> how many jobs hit it. A job that fails three rules is
    # counted under all three, so these sum past `failed` on purpose.
    reasons: dict[str, int] = dataclasses.field(default_factory=dict)

    def summary(self) -> str:
        markets = " ".join(f"{k}={v}" for k, v in sorted(self.by_market.items())) or "-"
        return (
            f"rank: scored={self.scored} passed={self.passed} failed={self.failed} "
            f"already_scored={self.skipped} [{markets}]"
        )

    def snapshot(self) -> DeterministicResult:
        """A copy safe to hand to another thread while the pass continues."""
        return dataclasses.replace(
            self, by_market=dict(self.by_market), reasons=dict(self.reasons)
        )


def run_deterministic(
    config: Config,
    market: str | None = None,
    limit: int | None = None,
    rescore: bool = False,
    rates: dict[str, float] | None = None,
    progress: Callable[[DeterministicResult], None] | None = None,
) -> DeterministicResult:
    """Stage 1 over every unscored active job. Cheap enough to run on everything.

    `rates` is one exchange-rate snapshot for this run. It is passed in rather
    than read from disk so every job in a run is compared against the same
    numbers, and so a stale file can never quietly become the rule.

    `progress` is called with a snapshot every `PROGRESS_EVERY` rows and once at
    the end, so a caller watching a run can show the counts moving rather than
    a blank panel for the length of the pass.
    """
    filters = deterministic.load_filters(config)
    if rates:
        for profile in (filters.get("profiles") or {}).values():
            salary = profile.get("salary")
            if isinstance(salary, dict):
                salary["rates"] = rates
    result = DeterministicResult()
    passed_by_market: dict[str, int] = {}

    with session_scope(config.db_path) as session:
        stmt = (
            select(Job, Company)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.is_active.is_(True))
            # Only canonical rows. Scoring every duplicate would multiply the
            # LLM batch by the number of sources a job appears on.
            .where((Job.canonical_job_id == Job.id) | (Job.canonical_job_id.is_(None)))
        )
        if market:
            stmt = stmt.where(Job.market == market)
        stmt = stmt.order_by(Job.first_seen_at.desc(), Job.id.desc())
        if limit:
            stmt = stmt.limit(limit)

        rows = session.execute(stmt).all()
        result.corpus = len(rows)
        seen = 0
        for job, company in rows:
            seen += 1
            if progress and seen % PROGRESS_EVERY == 0:
                progress(result.snapshot())
            score = _score_row(session, job.id, job.market)
            if score is not None and not rescore and score.deterministic_notes is not None:
                result.skipped += 1
                continue

            verdict = deterministic.evaluate(job, company, filters)
            if score is None:
                score = Score(job_id=job.id, profile=job.market)
                session.add(score)
            score.deterministic_pass = verdict.passed
            score.deterministic_notes = verdict.as_notes()
            score.scored_at = utcnow()
            if verdict.tz_overlap_hours is not None:
                job.tz_min_overlap_h = verdict.tz_overlap_hours

            result.scored += 1
            if verdict.passed:
                result.passed += 1
                passed_by_market[job.market] = passed_by_market.get(job.market, 0) + 1
            else:
                result.failed += 1
            for code in verdict.codes:
                result.reasons[code] = result.reasons.get(code, 0) + 1

    result.by_market = passed_by_market
    if progress:
        progress(result.snapshot())
    return result


def _score_row(session, job_id: int, market: str) -> Score | None:
    return session.scalars(
        select(Score).where(Score.job_id == job_id, Score.profile == market)
    ).first()


# --- stage 2, over a file -----------------------------------------------------


def emit(
    config: Config,
    path: pathlib.Path,
    market: str | None = None,
    limit: int = DEFAULT_BATCH,
    max_per_company: int = MAX_PER_COMPANY,
    regate: bool = False,
) -> dict[str, Any]:
    """Write a self-contained batch for the gate: prompt, profile, and jobs.

    `regate` includes jobs that already carry a verdict. Prompts and stated
    constraints change, and a verdict produced under criteria the user has since
    disagreed with should not be frozen in place.
    """
    filters = deterministic.load_filters(config)
    candidate = profile.load(config)
    batches: dict[str, list[dict[str, Any]]] = {}

    with session_scope(config.db_path) as session:
        stmt = (
            select(Job, Company, Score)
            .join(Score, Score.job_id == Job.id)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.is_active.is_(True))
            .where(Score.deterministic_pass.is_(True))
        )
        if not regate:
            stmt = stmt.where(Score.llm_score.is_(None))
        if market:
            stmt = stmt.where(Job.market == market)
        # Over-fetch, then thin by company, so the cap does not just truncate
        # the newest company's postings off the end of the batch.
        # When re-gating, the highest previous scores go first: those are the
        # verdicts most likely to change a decision.
        if regate:
            stmt = stmt.order_by(Score.llm_score.desc().nullslast(), Job.id.desc())
        else:
            stmt = stmt.order_by(Job.first_seen_at.desc(), Job.id.desc())
        stmt = stmt.limit(limit * 8)

        per_company: dict[Any, int] = {}
        taken = 0
        held = 0
        for job, company, score in session.execute(stmt).all():
            if taken >= limit:
                break
            key = company.id if company else f"job:{job.id}"
            if max_per_company and per_company.get(key, 0) >= max_per_company:
                held += 1
                continue
            per_company[key] = per_company.get(key, 0) + 1
            taken += 1
            batches.setdefault(job.market, []).append(_gate_record(job, company, score))

    payload = {
        "generated_at": utcnow().isoformat(),
        "candidate_profile": candidate,
        "response_contract": {
            "format": "json_array",
            "item": {
                "job_id": "int, echo it back exactly",
                "score": "float 0.0 to 1.0",
                "reasoning": "one sentence, persisted, written for a human",
                "red_flags": "list of short strings, only what the posting states",
            },
        },
        "batches": [
            {
                "market": market_name,
                "prompt": _prompt_text(config, filters, market_name, candidate),
                "min_score_to_surface": (
                    deterministic.profile_for(filters, market_name).get("min_score_to_surface")
                ),
                "jobs": jobs,
            }
            for market_name, jobs in sorted(batches.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "batches": len(payload["batches"]),
        "jobs": sum(len(b) for b in batches.values()),
        # Jobs skipped so one company could not fill the batch. Not a rejection:
        # they are first in line next run.
        "held_by_company_cap": held,
    }


def _gate_record(job: Job, company: Company | None, score: Score) -> dict[str, Any]:
    description = job.description_en_md or job.description_md or job.description_text or ""
    return {
        "job_id": job.id,
        "title": job.title,
        "company": company.name if company else None,
        "company_one_liner": None,
        "yc_batch": company.yc_batch if company else None,
        "location": job.location_raw,
        "country": job.country,
        "remote_type": job.remote_type,
        "employment_type": job.employment_type,
        "seniority": job.seniority,
        "salary": _salary_text(job),
        "posted_at": job.posted_at.date().isoformat() if job.posted_at else None,
        "source": job.source,
        "jd_completeness": job.jd_completeness,
        "tz_overlap_hours": job.tz_min_overlap_h,
        "deterministic_boost": (score.deterministic_notes or {}).get("boost"),
        "description": description[:DESCRIPTION_CHARS],
        "description_truncated": len(description) > DESCRIPTION_CHARS,
    }


def _salary_text(job: Job) -> str | None:
    if not job.salary_is_stated or job.salary_min is None:
        return None
    high = job.salary_max or job.salary_min
    currency = job.salary_currency or ""
    period = job.salary_period or ""
    return f"{int(job.salary_min):,}-{int(high):,} {currency}/{period}".strip("/")


# What `{location}` becomes when the candidate's own location cannot be
# established. The gate is told plainly rather than left to assume one, because
# an assumed home country turns every posting elsewhere into a rejection.
UNKNOWN_LOCATION = "not stated — do not assume one, and do not score on location"


def _prompt_text(
    config: Config, filters: dict[str, Any], market: str, candidate: str
) -> str:
    """The market's gate prompt with the candidate substituted in."""
    relative = deterministic.profile_for(filters, market).get("llm_gate_prompt")
    if not relative:
        return ""
    user_path = config.home / relative
    packaged = deterministic.PACKAGED_FILTERS.parent / relative
    source = user_path if user_path.exists() else packaged
    if not source.exists():
        return ""
    where = profile.location(config)
    return (
        source.read_text(encoding="utf-8")
        .replace("{profile}", candidate)
        # `{location}` is where they live, "Istanbul, Turkey"; `{country}` is the
        # country alone, because every rule that turns on location compares
        # countries and a city in that slot makes the rule unusable.
        .replace("{location}", where.text if where else UNKNOWN_LOCATION)
        .replace("{country}", where.country_name if where else UNKNOWN_LOCATION)
    )


@dataclasses.dataclass
class IngestResult:
    read: int = 0
    written: int = 0
    unknown: int = 0
    invalid: int = 0

    def summary(self) -> str:
        return (
            f"rank ingest: read={self.read} written={self.written} "
            f"unknown_job_ids={self.unknown} invalid={self.invalid}"
        )


def ingest(config: Config, path: pathlib.Path, model: str | None = None) -> IngestResult:
    """Write gate verdicts back. Tolerant of shape, strict about values.

    The file is produced by a model, so it accepts either a bare array or the
    emitted envelope, but a score outside 0..1 or a job id that is not in the
    corpus is counted and skipped rather than written. Persisting a hallucinated
    job id would be worse than losing a verdict.
    """
    result = IngestResult()
    raw = json.loads(path.read_text(encoding="utf-8"))
    verdicts = _flatten_verdicts(raw)
    result.read = len(verdicts)

    with session_scope(config.db_path) as session:
        for verdict in verdicts:
            job_id = verdict.get("job_id")
            score_value = verdict.get("score")
            try:
                job_id = int(job_id)
                score_value = float(score_value)
            except (TypeError, ValueError):
                result.invalid += 1
                continue
            if not 0.0 <= score_value <= 1.0:
                result.invalid += 1
                continue

            job = session.get(Job, job_id)
            if job is None:
                result.unknown += 1
                continue

            row = _score_row(session, job_id, job.market)
            if row is None:
                row = Score(job_id=job_id, profile=job.market, deterministic_pass=True)
                session.add(row)
            row.llm_score = score_value
            row.llm_reasoning = _reasoning(verdict)
            row.llm_model = model or verdict.get("model") or "claude-code"
            row.scored_at = utcnow()
            result.written += 1
    return result


def _flatten_verdicts(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("verdicts", "results", "jobs", "scores"):
            if isinstance(raw.get(key), list):
                return [item for item in raw[key] if isinstance(item, dict)]
        collected: list[dict[str, Any]] = []
        for batch in raw.get("batches") or []:
            if isinstance(batch, dict):
                collected.extend(_flatten_verdicts(batch))
        return collected
    return []


def _reasoning(verdict: dict[str, Any]) -> str:
    text = str(verdict.get("reasoning") or "").strip()
    flags = verdict.get("red_flags")
    if isinstance(flags, list) and flags:
        joined = ", ".join(str(flag) for flag in flags[:6])
        text = f"{text} [red flags: {joined}]".strip()
    return text or "(no reasoning returned)"
