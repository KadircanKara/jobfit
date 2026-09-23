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
import functools
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


# Every drop reason carries a code alongside its sentence. The sentence names
# the specific job's problem ("salary 62,000 below 90,000"), which is what a
# person needs; the code is the family it belongs to, which is what a run
# summary can count. Without the code a histogram would have one bar per
# distinct salary figure.
REASON_LABELS: dict[str, str] = {
    "source_excluded": "source not selected",
    "age": "older than the age limit",
    "company_blocked": "company blocklisted",
    "title_excluded": "title matches an excluded pattern",
    "title_unmatched": "title matches no required pattern",
    "field_mismatch": "work model, type or country not allowed",
    "phrase_excluded": "excluded phrase in the posting",
    "seniority_low": "seniority below the floor",
    "seniority_high": "seniority above the ceiling",
    "salary_unstated": "salary not stated",
    "salary_below": "salary below the floor",
    "rate_below": "freelance rate below the floor",
    "client_no_spend": "client has never spent anything",
    "client_low_spend": "client spent less than the minimum",
    "client_unverified": "client has not verified payment",
    "tz_overlap": "timezone overlap below the minimum",
}

# Reasons the Filters panel can actually move. The rest live in filters.yaml,
# so telling the user to adjust them from the browser would be a lie.
TUNABLE_REASONS = frozenset(
    {"age", "title_unmatched", "field_mismatch", "seniority_low", "seniority_high",
     "salary_unstated", "salary_below", "rate_below",
     "client_no_spend", "client_low_spend", "client_unverified"}
)


@dataclasses.dataclass
class Verdict:
    """Why a job passed or failed. The reasons are persisted, not just counted."""

    passed: bool
    reasons: list[str] = dataclasses.field(default_factory=list)
    codes: list[str] = dataclasses.field(default_factory=list)
    boost: float = 1.0
    tz_overlap_hours: float | None = None

    def drop(self, code: str, reason: str) -> None:
        """Record one failing rule, as both a family and a sentence."""
        self.codes.append(code)
        self.reasons.append(reason)

    def as_notes(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "codes": self.codes,
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

    _check_source(job, global_rules, verdict)
    _check_age(job, global_rules, now, verdict)
    _check_excluded_company(company, global_rules, verdict)
    _check_excluded_titles(job, global_rules, verdict)
    _check_required_titles(job, global_rules, profile, verdict)
    _check_hard_requires(job, profile, verdict)
    _check_hard_excludes(haystack, profile, verdict)
    _check_seniority(job, profile, verdict)
    _check_salary(job, profile, verdict)
    _check_rate(job, profile, verdict)
    _check_client(job, profile, verdict)
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


def _check_source(job: Job, rules: dict[str, Any], verdict: Verdict) -> None:
    """Drop anything the user did not ask to draw from.

    Absent means unrestricted: a filter document written before this key existed
    must keep meaning "every source", never "no source".
    """
    allowed = rules.get("sources")
    # Key absent means unrestricted, so a document written before this key existed
    # keeps meaning "every source". Key present and empty is a different statement -
    # nothing was selected - and must drop everything rather than quietly re-open
    # the corpus the fetch pass just refused to fill.
    if allowed is None:
        return
    if job.source not in set(allowed):
        verdict.drop("source_excluded", f"source {job.source} is not selected")


def _check_age(job: Job, rules: dict[str, Any], now: dt.datetime, verdict: Verdict) -> None:
    max_age = rules.get("max_age_days")
    if not max_age or not job.posted_at:
        return
    age = (now - job.posted_at).days
    if age > int(max_age):
        verdict.drop("age", f"older than {max_age} days ({age})")


def _check_excluded_company(company: Company | None, rules: dict[str, Any], verdict: Verdict) -> None:
    blocked = [str(name).strip().lower() for name in (rules.get("exclude_companies") or [])]
    if not blocked or company is None:
        return
    if (company.name or "").strip().lower() in blocked or company.normalized_name in blocked:
        verdict.drop("company_blocked", f"company blocklisted ({company.name})")


def _check_excluded_titles(job: Job, rules: dict[str, Any], verdict: Verdict) -> None:
    for pattern in rules.get("exclude_titles_regex") or []:
        try:
            compiled = re.compile(pattern)
        except re.error:
            # A broken regex in a user-edited file must not take the run down.
            continue
        if compiled.search(job.title or ""):
            verdict.drop("title_excluded", f"title matches {pattern}")


def _check_required_titles(
    job: Job, global_rules: dict[str, Any], profile: dict[str, Any], verdict: Verdict
) -> None:
    """At least one pattern must match the title.

    Added after watching a real batch: stage 1 was handing the gate telehealth
    doctors, Somali QA testers, and a construction estimator. Those cost a gate
    call each to reject something a regex settles for free. The profile's list
    overrides the global one so a market can widen or narrow it.
    """
    patterns = profile.get("require_titles_regex") or global_rules.get("require_titles_regex")
    if not patterns:
        return
    title = job.title or ""
    for pattern in patterns:
        try:
            if re.search(pattern, title):
                return
        except re.error:
            continue
    verdict.drop("title_unmatched", "title matches no required pattern")


def _check_hard_requires(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    # A posting that says "Remote, Worldwide" is open to the user wherever the
    # company happens to be incorporated, so a country allow-list must not reject
    # it on the head office. Only set when the user asked for worldwide.
    worldwide_ok = bool(profile.get("allow_worldwide")) and timezones.is_worldwide(
        job.location_raw
    )
    for field, allowed in (profile.get("hard_requires") or {}).items():
        if field == "country" and worldwide_ok:
            continue
        value = getattr(job, field, None)
        if value in (None, "", "unknown"):
            continue  # unknown is never a rejection
        allowed_values = allowed if isinstance(allowed, list) else [allowed]
        if str(value) not in {str(item) for item in allowed_values}:
            verdict.drop("field_mismatch", f"{field}={value} not in {allowed_values}")


def _check_hard_excludes(haystack: str, profile: dict[str, Any], verdict: Verdict) -> None:
    for phrase in profile.get("hard_excludes") or []:
        text = str(phrase).strip().lower()
        if text and _phrase_pattern(text).search(haystack):
            verdict.drop("phrase_excluded", f"excluded phrase {text!r}")


@functools.lru_cache(maxsize=256)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Match a phrase as whole words, not as a substring.

    Found live: the "W2" exclusion fired on 11 jobs because "w2" appears inside
    unrelated tokens. Lookarounds rather than \\b so a phrase that starts or ends
    with punctuation still behaves.
    """
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


def _check_seniority(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    minimum = profile.get("seniority_min")
    maximum = profile.get("seniority_max")

    if not job.seniority or job.seniority not in SENIORITY_ORDER:
        # Unknown is normally not a rejection: most postings never state a level,
        # and dropping them all would empty the corpus. Asking for exactly one
        # level is a different request, though - "internships, nothing else"
        # cannot be honoured while every unclassified senior role sails through -
        # so a band of one level, and only that, reads unknown as a miss.
        if minimum and minimum == maximum and minimum in SENIORITY_ORDER:
            stated = job.seniority or "unstated"
            verdict.drop("seniority_unstated", f"seniority {stated} is not {minimum}")
        return

    level = SENIORITY_ORDER.index(job.seniority)

    if minimum in SENIORITY_ORDER and level < SENIORITY_ORDER.index(str(minimum)):
        verdict.drop("seniority_low", f"seniority {job.seniority} below {minimum}")

    # A ceiling is not symmetry for its own sake: a senior engineer applying to a
    # principal or VP-level posting wastes a gate call and an application.
    if maximum in SENIORITY_ORDER and level > SENIORITY_ORDER.index(str(maximum)):
        verdict.drop("seniority_high", f"seniority {job.seniority} above {maximum}")


# Everything is compared as an annual figure. A job stating a monthly or hourly
# band is converted with these, which are hours and months, not exchange rates.
#
# "fixed" (a freelance project budget) is deliberately absent here, not a gap
# to fill in later: there is no number of fixed-price projects per year that
# means anything, so a project budget must never be annualised. `_check_salary`
# treats a missing factor as "unknown" and returns, which is exactly what a
# fixed-price figure is to a salaried floor.
_PERIOD_TO_ANNUAL = {
    "annual": 1.0, "monthly": 12.0, "weekly": 52.0, "daily": 260.0, "hourly": 2080.0,
}


def _check_salary(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    """Reject a stated salary below the floor.

    There is still no hardcoded FX table: a rate written down once is wrong
    later and would drop jobs with no visible cause. A run may pass a rate
    snapshot it fetched itself under `salary.rates`, and only then is a figure
    in another currency converted. Without a snapshot, or for a currency the
    snapshot does not cover, the figure stays unknown — and unknown is never a
    rejection, unless the profile says include_unstated: false.
    """
    rules = profile.get("salary") or {}
    floor = rules.get("min_annual")
    if floor is None:
        return
    wanted_currency = str(rules.get("currency") or "").upper() or None
    include_unstated = rules.get("include_unstated", True)

    if not job.salary_is_stated or job.salary_min is None:
        if not include_unstated:
            verdict.drop("salary_unstated", "salary not stated")
        return

    job_currency = (job.salary_currency or "").upper() or None
    rates = rules.get("rates") or {}

    factor = _PERIOD_TO_ANNUAL.get((job.salary_period or "annual").lower())
    if factor is None:
        return
    # Compare the top of the band: a job paying 80k-120k clears a 100k floor.
    top = float(job.salary_max or job.salary_min) * factor

    if wanted_currency and job_currency and job_currency != wanted_currency:
        converted = _convert(top, job_currency, wanted_currency, rates)
        if converted is None:
            return  # no usable rate, so the figure stays unknown
        if converted < float(floor):
            verdict.drop(
                "salary_below",
                f"salary {top:,.0f} {job_currency} "
                f"({converted:,.0f} {wanted_currency}) below {float(floor):,.0f} {wanted_currency}",
            )
        return

    if top < float(floor):
        verdict.drop("salary_below", f"salary {top:,.0f} below {float(floor):,.0f}")


def _convert(amount: float, frm: str, to: str, rates: dict[str, Any]) -> float | None:
    """`amount` in `frm`, expressed in `to`, or None when the rates cannot say."""
    try:
        source = float(rates[frm])
        target = float(rates[to])
    except (KeyError, TypeError, ValueError):
        return None
    if source <= 0:
        return None
    return amount / source * target


def _check_rate(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    """Judge a freelance rate against its own floor, never the salary floor.

    An hourly rate and an annual salary are not the same quantity, and 2080
    hours is a fiction for contract work: a $60/hr gig is not a $125k offer.
    """
    rules = profile.get("rate") or {}
    period = (job.salary_period or "").lower()
    floor = rules.get("min_hourly") if period == "hourly" else rules.get("min_fixed")
    if not floor or period not in ("hourly", "fixed"):
        return
    top = job.salary_max or job.salary_min
    if top is None:
        return  # unstated is never a rejection
    if float(top) < float(floor):
        verdict.drop("rate_below", f"{period} rate {top:g} below {float(floor):g}")


def _check_client(job: Job, profile: dict[str, Any], verdict: Verdict) -> None:
    """Judge who is paying, when the user has asked to be picky about it.

    Every rule here defaults off, so a filters.yaml written before they existed
    keeps exactly the corpus it had. Both follow the house rule that unknown is
    never a rejection: `client_verified` and `client_total_spent` are null on
    every source but Upwork, and null there means the fetch never learned the
    value - not that the client is unverified or has spent nothing. Reading a
    null as a zero would silently drop the entire rest of the corpus the moment
    either switch was turned on.
    """
    rules = profile.get("client") or {}

    floor = rules.get("min_spend")
    spent = job.client_total_spent
    if floor and spent is not None and spent < float(floor):
        verdict.drop("client_low_spend", f"client has spent ${spent:,.0f}, under ${float(floor):,.0f}")
    elif rules.get("require_spend") and spent is not None and spent <= 0:
        # A filters.yaml written before `min_spend` existed and not saved since.
        verdict.drop("client_no_spend", "client has never spent on this platform")

    if rules.get("require_verified") and job.client_verified is False:
        verdict.drop("client_unverified", "client has not verified a payment method")


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
        verdict.drop("tz_overlap", f"timezone overlap {overlap:g}h below {minimum}h")


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
