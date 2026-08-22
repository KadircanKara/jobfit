"""Hand off to the existing tailoring-cv skill. Do not reimplement it.

The contract here follows the skill as it actually is on disk, not PLAN.md
section 10, and the difference was confirmed with the user:

- The **skill** owns the folder, and its own scripts hardcode
  `~/Career/Job_Applications/Tailored CVs/<Company> - <Position> @ <Location>/`.
  PLAN.md proposed `{date}-{company-slug}-{title-slug}`. The skill wins: jobhunt
  conforms to the tool that already works. The location suffix was added to both
  at once, on 2026-08-22; changing it here alone would split the two apart.
- The skill reads the posting from **`jd.txt`**, verbatim. PLAN.md proposed
  `job.md`. Again the skill wins.
- `job.json` is written alongside as a sidecar. The skill does not read it; it
  exists so jobhunt can answer "what did I apply to and why" later, and so a
  future version of the skill can pick it up without another migration.

Mode is `handoff` by default: print the instruction and stop. The skill has its
own approval gate that needs a human at it, so shelling out to it would defeat
the human-in-the-loop requirement rather than automate it.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any

from jobhunt.config import Config
from jobhunt.db.models import Company, Job, Score

_UNSAFE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_WS = re.compile(r"\s+")


@dataclasses.dataclass
class Handoff:
    folder: pathlib.Path
    jd_file: str
    files: list[str]
    instruction: str
    mode: str


# Postings repeat themselves in the location field: "Singapore, Singapore,
# Singapore" and "Ho Chi Minh City, Ho Chi Minh City, Vietnam" are both real.
# The folder says each part once.
_UNKNOWN_PLACES = {"", "n a", "na", "unknown", "remote", "-"}


def folder_name(company: str, title: str, location: str | None = None) -> str:
    """`<Company> - <Position> @ <Location>`, spelled as the posting spells them.

    Only characters a filesystem cannot take are removed. Deliberately not
    slugified: the skill's own scripts cd into this folder by the same name, and
    the user reads it. The separator stays " - " so a hyphen inside a company
    name ("Île-de-France GmbH") does not read as the separator.

    A posting with no usable location keeps the older two-part name rather than
    growing an empty suffix.
    """
    company = _clean(company) or "Unknown"
    title = _clean(title) or "Role"
    where = place(location)
    return f"{company} - {title} @ {where}" if where else f"{company} - {title}"


def place(location: str | None) -> str:
    """The location as a folder can carry it, or "" when it says nothing.

    Slashes matter here beyond tidiness: "São Paulo / SP / Brasil" is a real
    value, and unstripped it would make three nested directories instead of one
    folder.
    """
    cleaned = _clean(location or "")
    seen: list[str] = []
    for part in cleaned.split(","):
        part = part.strip()
        if not part or part.lower() in _UNKNOWN_PLACES:
            continue
        if part.lower() not in {kept.lower() for kept in seen}:
            seen.append(part)
    return ", ".join(seen)[:70]


def _clean(value: str) -> str:
    value = _UNSAFE.sub(" ", value or "")
    value = _WS.sub(" ", value).strip().strip(".")
    return value[:120]


def prepare(
    config: Config,
    job: Job,
    company: Company | None,
    score: Score | None,
    dry_run: bool = False,
) -> Handoff:
    """Write the interface files. Never overwrites an existing folder."""
    root = pathlib.Path(str(config.get("tailoring", "applications_root"))).expanduser()
    folder = root / folder_name(
        company.name if company else job.source, job.title, job.location_raw
    )
    jd_filename = str(config.get("tailoring", "jd_filename", default="jd.txt"))
    target_language = str(config.get("tailoring", "jd_language", default="en"))
    mode = str(config.get("tailoring", "mode", default="handoff"))

    body = job.description_md or job.description_text or ""
    language = job.description_lang or "en"
    english = job.description_en_md

    written: list[str] = []
    if not dry_run:
        folder.mkdir(parents=True, exist_ok=True)

    # Which text the skill reads is a config decision, resolved here so the
    # skill reads one filename and never has to know the language rules.
    if language != "en" and target_language == "en" and english:
        _write(folder / jd_filename, english, dry_run)
        original = f"jd.{language}.txt"
        _write(folder / original, body, dry_run)
        written += [jd_filename, original]
    else:
        # A non-English JD with no translation yet goes through as-is. The
        # sidecar states jd_language, so the skill is told rather than left to
        # guess why the text is Turkish.
        _write(folder / jd_filename, body, dry_run)
        written.append(jd_filename)

    sidecar = _sidecar(job, company, score, jd_filename, language)
    _write(folder / "job.json", json.dumps(sidecar, ensure_ascii=False, indent=2), dry_run)
    written.append("job.json")

    return Handoff(
        folder=folder,
        jd_file=jd_filename,
        files=written,
        instruction=f"Tailor my CV for the job in {folder}",
        mode=mode,
    )


def _write(path: pathlib.Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _sidecar(
    job: Job,
    company: Company | None,
    score: Score | None,
    jd_filename: str,
    language: str,
) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "title": job.title,
        "company": company.name if company else None,
        "company_domain": company.domain if company else None,
        "location": job.location_raw,
        "remote_type": job.remote_type,
        "market": job.market,
        "seniority": job.seniority,
        "role_family": job.role_family,
        "salary": _salary(job),
        "posted_at": job.posted_at.date().isoformat() if job.posted_at else None,
        "apply_url": job.apply_url,
        "source": job.source,
        "yc_batch": company.yc_batch if company else None,
        "match_score": score.llm_score if score else None,
        "match_reasoning": score.llm_reasoning if score else None,
        "jd_file": jd_filename,
        "jd_language": language,
        "jd_source": job.jd_source,
        "jd_quality_score": job.jd_quality_score,
    }


def _salary(job: Job) -> str | None:
    if not job.salary_is_stated or job.salary_min is None:
        return None
    high = int(job.salary_max or job.salary_min)
    period = job.salary_period or "year"
    return f"{int(job.salary_min):,}-{high:,} {job.salary_currency or ''}/{period}".strip()
