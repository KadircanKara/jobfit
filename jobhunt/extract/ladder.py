"""Rungs 1, 2, and 5, in order, stopping at the first that clears the gate.

Rungs 3 (browser) and 4 (LLM extractor) are phase 7, when the Turkish sources
arrive. Every source in the corpus today returns a full description in its
listing call, so today the ladder mostly exists to enforce the completeness gate
rather than to do heavy lifting. That is deliberate: the gate is the part that
protects the tailoring skill from a truncated JD.

`jd_source` records which rung won, because that field is how a quality
complaint gets debugged three weeks later.
"""
from __future__ import annotations

import dataclasses

from jobhunt.config import Config
from jobhunt.db.models import Job, utcnow
from jobhunt.extract import fetcher, jsonld, quality, readability
from jobhunt.pipeline import normalize as norm


@dataclasses.dataclass
class Result:
    job_id: int
    rung: str | None = None
    completeness: str = "none"
    quality_score: float = 0.0
    chars: int = 0
    origin: str | None = None
    reasons: list[str] = dataclasses.field(default_factory=list)
    changed: bool = False

    def summary(self) -> str:
        return (
            f"jd {self.job_id}: rung={self.rung or 'none'} completeness={self.completeness} "
            f"quality={self.quality_score:.2f} chars={self.chars} "
            f"fetch={self.origin or '-'}"
            + (f" reasons={'; '.join(self.reasons)}" if self.reasons else "")
        )


def run(config: Config, session, job: Job, force: bool = False) -> Result:
    """Fill in a job's description. Writes to the job row, does not commit."""
    result = Result(job_id=job.id)

    if job.jd_completeness == "full" and not force:
        assessment = quality.assess(job.description_text)
        result.rung = job.jd_source
        result.completeness = "full"
        result.quality_score = job.jd_quality_score or assessment.score
        result.chars = assessment.chars
        return result

    # Rung 0 in all but name: whatever the listing already gave us. Free, and on
    # this corpus it is usually the answer.
    existing = quality.assess(job.description_text or job.description_md)
    if existing.passed and not force:
        _apply(job, job.description_html, job.description_md, job.description_text,
               job.jd_source or "api", existing)
        return _fill(result, "api", existing, None)

    url = job.source_url or job.apply_url
    if not url:
        result.reasons = ["no detail url"] + existing.reasons
        return result

    html, origin = fetcher.fetch(config, job.source, job.id, url, force=force)
    result.origin = origin
    if not html:
        result.reasons = [origin] + existing.reasons
        return result

    # Rung 1: JSON-LD.
    found = jsonld.extract(html)
    if found and found.usable:
        assessment = quality.assess(found.description_text)
        if assessment.passed:
            markdown = norm.html_to_markdown(found.description_html)
            _apply(job, found.description_html, markdown, found.description_text,
                   "jsonld", assessment)
            return _fill(result, "jsonld", assessment, origin)
        result.reasons += [f"jsonld: {reason}" for reason in assessment.reasons]

    # Rung 2: readability.
    markdown, text = readability.extract(html, url=url)
    if text:
        assessment = quality.assess(text)
        if assessment.passed:
            _apply(job, html, markdown, text, "readability", assessment)
            return _fill(result, "readability", assessment, origin)
        result.reasons += [f"readability: {reason}" for reason in assessment.reasons]

    result.reasons = result.reasons or existing.reasons
    return result


def paste(session, job: Job, text: str) -> Result:
    """Rung 5: the user pasted the JD in.

    Not an admission of defeat. It keeps a hostile source usable and costs the
    user fifteen seconds on a job they already decided to apply to.
    """
    result = Result(job_id=job.id)
    body = (text or "").strip()
    assessment = quality.assess(body)
    if not body:
        result.reasons = ["empty paste"]
        return result
    markdown = body if "<" not in body[:200] else norm.html_to_markdown(body)
    plain = norm.html_to_text(body) if "<" in body[:200] else body
    _apply(job, None, markdown, plain, "manual_paste", assessment)
    # A human pasted this. It is full by definition, even if the heuristics
    # dislike it, because the person who will apply to the job chose the text.
    job.jd_completeness = "full"
    return _fill(result, "manual_paste", assessment, "paste")


def _apply(
    job: Job,
    html: str | None,
    markdown: str | None,
    text: str | None,
    source: str,
    assessment: quality.Quality,
) -> None:
    if html:
        job.description_html = html
    if markdown:
        job.description_md = markdown
    if text:
        job.description_text = text
    job.jd_source = source
    job.jd_quality_score = assessment.score
    job.jd_completeness = "full" if assessment.passed else norm.completeness(text)
    job.jd_extracted_at = utcnow()


def _fill(result: Result, rung: str, assessment: quality.Quality, origin: str | None) -> Result:
    result.rung = rung
    result.completeness = "full" if assessment.passed else "snippet"
    result.quality_score = assessment.score
    result.chars = assessment.chars
    result.origin = origin
    result.reasons = assessment.reasons
    result.changed = True
    return result
