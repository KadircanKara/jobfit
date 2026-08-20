"""Stage 1: the deterministic filter. Free, and it runs on everything.

Driven entirely by filters.yaml so the rules can be tuned without touching code,
which matters because they will be tuned constantly for the first week.

Design rule throughout: an unknown value is never a reason to drop a job. A
missing country, a missing seniority, a missing date are all "no objection".
Aggregators drop fields that the company's own board states fully, so filtering
on absence would quietly discard exactly the postings the corpus works hardest
to collect. A job wrongly kept costs one LLM call. A job wrongly dropped is
never seen again.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import re
from typing import Any

import yaml

from jobhunt.config import Config
from jobhunt.db.models import Company, Job, utcnow
from jobhunt.rank import timezones

# Ordered weakest to strongest. A job whose level is unknown passes any minimum.
SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "staff", "lead", "principal"]

FILTERS_FILENAME = "filters.yaml"
PACKAGED_FILTERS = pathlib.Path(__file__).resolve().parent.parent / "assets" / FILTERS_FILENAME


@dataclasses.dataclass
class Verdict:
    """Why a job passed or failed. The reasons are persisted, not just counted."""

    passed: bool
    reasons: list[str] = dataclasses.field(default_factory=list)
    boost: float = 1.0
    tz_overlap_hours: float | None = None

    def as_notes(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "boost": round(self.boost, 3),
            "tz_overlap_hours": self.tz_overlap_hours,
        }


def load_filters(config: Config) -> dict[str, Any]:
    """User's filters.yaml, falling back to the packaged default."""
    path = config.home / FILTERS_FILENAME
    source = path if path.exists() else PACKAGED_FILTERS
    return yaml.safe_load(source.read_text(encoding="utf-8")) or {}


def profile_for(filters: dict[str, Any], market: str) -> dict[str, Any]:
    return (filters.get("profiles") or {}).get(market) or {}


def evaluate(
    job: Job,
    company: Company | None,
    filters: dict[str, Any],
    now: dt.datetime | None = None,
) -> Verdict:
    """Apply every rule for this job's market. One pass, no I/O."""
    now = now or utcnow()
    verdict = Verdict(passed=True)
    global_rules = filters.get("global") or {}
    profile = profile_for(filters, job.market)

    haystack = _haystack(job)

    _check_age(job, global_rules, now, verdict)
    _check_excluded_company(company, global_rules, verdict)
    _check_excluded_titles(job, global_rules, verdict)
    _check_hard_requires(job, profile, verdict)
    _check_hard_excludes(haystack, profile, verdict)
    _check_seniority(job, profile, verdict)
    _check_timezone(job, profile, verdict)
    _apply_boosts(job, company, profile, verdict)

    verdict.passed = not verdict.reasons
    return verdict


def _haystack(job: Job) -> str:
    """Title plus the first slice of the description.

    Only the first 4000 characters: the phrases these rules look for ("must
    reside in", "US only") appear in the eligibility paragraph near the top, and
    scanning a whole 40 KB body would match the same words inside an unrelated
    benefits or legal footer.
    """
    body = (job.description_text or job.description_md or "")[:4000]
    return f"{job.title}\n{job.location_raw or ''}\n{body}".lower()


def _check_age(job: Job, rules: dict[str, Any], now: dt.datetime, verdict: Verdict) -> None:
    max_age = rules.get("max_age_days")
    if not max_age or not job.posted_at:
        return
    age = (now - job.posted_at).days
    if age > int(max_age):
        verdict.reasons.append(f"older than {max_age} days ({age})")


def _check_excluded_company(company: Company | None, rules: dict[str, Any], verdict: Verdict) -> None:
    blocked = [str(name).strip().lower() for name in (rules.get("exclude_companies") or [])]
    if not blocked or company is None:
        return
    if (company.name or "").strip().lower() in blocked or company.normalized_name in blocked:
        verdict.reasons.append(f"company blocklisted ({company.name})")


def _check_excluded_titles(job: Job, rules: dict[str, Any], verdict: Verdict) -> None:
    for pattern in rules.get("exclude_titles_regex") or []:
        try:
            compiled = re.compile(pattern)
        except re.error:
            # A broken regex in a user-edited file must not take the run down.
            continue
        if compiled.search(job.title or ""):
            verdict.reasons.append(f"title matches {pattern}")


def _check_hard_requires(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    for field, allowed in (profile.get("hard_requires") or {}).items():
        value = getattr(job, field, None)
        if value in (None, "", "unknown"):
            continue  # unknown is never a rejection
        allowed_values = allowed if isinstance(allowed, list) else [allowed]
        if str(value) not in {str(item) for item in allowed_values}:
            verdict.reasons.append(f"{field}={value} not in {allowed_values}")


def _check_hard_excludes(haystack: str, profile: dict[str, Any], verdict: Verdict) -> None:
    for phrase in profile.get("hard_excludes") or []:
        text = str(phrase).strip().lower()
        if text and text in haystack:
            verdict.reasons.append(f"excluded phrase {text!r}")


def _check_seniority(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    minimum = profile.get("seniority_min")
    if not minimum or not job.seniority:
        return
    if minimum not in SENIORITY_ORDER or job.seniority not in SENIORITY_ORDER:
        return
    if SENIORITY_ORDER.index(job.seniority) < SENIORITY_ORDER.index(str(minimum)):
        verdict.reasons.append(f"seniority {job.seniority} below {minimum}")


def _check_timezone(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    rules = profile.get("timezone") or {}
    minimum = rules.get("min_overlap_hours")
    if minimum is None:
        return
    if timezones.is_worldwide(job.location_raw):
        verdict.tz_overlap_hours = timezones.FULL_OVERLAP
        return
    base = timezones.base_offset_for(rules.get("base"))
    overlap = timezones.overlap_hours(job.country, base)
    verdict.tz_overlap_hours = overlap
    if overlap is None:
        return  # unknown country is not a rejection
    if overlap < float(minimum):
        verdict.reasons.append(f"timezone overlap {overlap:g}h below {minimum}h")


def _apply_boosts(
    job: Job, company: Company | None, profile: dict[str, Any], verdict: Verdict
) -> None:
    """Boosts order the shortlist. They never rescue a job that failed a rule."""
    boosts = profile.get("boost") or {}
    title = (job.title or "").lower()
    if boosts.get("founding_engineer") and "founding" in title:
        verdict.boost *= float(boosts["founding_engineer"])
    if boosts.get("tag_ai") and job.role_family == "ai_ml":
        verdict.boost *= float(boosts["tag_ai"])
    if boosts.get("recent_batch") and company is not None and _is_recent_batch(company.yc_batch):
        verdict.boost *= float(boosts["recent_batch"])


# YC batch codes look like "W21", "S24", "X25". Recent means the last four,
# resolved against the current year rather than hardcoded, so this does not rot.
_BATCH = re.compile(r"^([WSFX])(\d{2})$", re.I)


def _is_recent_batch(batch: str | None, now: dt.datetime | None = None) -> bool:
    if not batch:
        return False
    match = _BATCH.match(batch.strip())
    if not match:
        return False
    year = 2000 + int(match.group(2))
    current = (now or utcnow()).year
    return current - year <= 2


def install_user_copies(config: Config) -> list[pathlib.Path]:
    """Copy filters.yaml and the prompts into the user's home, once.

    Never overwrites. These are the two files meant to be tuned by hand, and an
    upgrade silently reverting a week of tuning would be the worst kind of bug.
    """
    written: list[pathlib.Path] = []
    assets = PACKAGED_FILTERS.parent
    for source in [PACKAGED_FILTERS, *sorted((assets / "prompts").glob("*.md"))]:
        target = config.home / source.relative_to(assets)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)
    return written
