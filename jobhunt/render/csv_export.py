"""The rolling jobs CSV.

One file, keyed by job_id, that survives every run. New jobs append, existing
rows refresh their score and metadata, and `applied` and `cv_status` are never
touched by an export. That last rule is the whole reason the file is safe to
regenerate: the user's marks are the one thing in it that no automated pass owns.

Written atomically, because an interrupted run truncating this file would lose
the record of what has been applied to.
"""
from __future__ import annotations

import csv
import dataclasses
import pathlib
from typing import Any

from jobhunt.config import Config
from jobhunt.db.models import utcnow

COLUMNS = [
    "job_id", "fit", "title", "company", "location", "work_model", "employment_type",
    "salary", "url", "source", "applied", "cv_status", "first_seen", "last_seen",
]

# Columns an export refreshes. Everything else in an existing row is left alone.
REFRESHED = {
    "fit", "title", "company", "location", "work_model", "employment_type",
    "salary", "url", "source", "last_seen",
}

TRUE = "TRUE"
FALSE = "FALSE"


@dataclasses.dataclass
class ExportResult:
    path: pathlib.Path
    added: int = 0
    refreshed: int = 0
    total: int = 0

    def summary(self) -> str:
        return (
            f"csv: added={self.added} refreshed={self.refreshed} "
            f"total={self.total} -> {self.path}"
        )


def csv_path(config: Config) -> pathlib.Path:
    configured = config.get("export", "csv_path")
    if configured:
        return pathlib.Path(str(configured)).expanduser()
    return config.home / "jobs.csv"


def read(path: pathlib.Path) -> dict[str, dict[str, str]]:
    """Existing rows by job_id. A missing or empty file is an empty dict."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return {row["job_id"]: row for row in rows if row.get("job_id")}


def row_from_card(card: Any, now: str) -> dict[str, str]:
    """One shortlist card as a CSV row. Fresh rows only; never used to update."""
    return {
        "job_id": str(card.job_id),
        "fit": f"{card.score:.2f}" if card.score is not None else "",
        "title": card.title,
        "company": card.company,
        "location": card.location,
        "work_model": card.remote_type,
        "employment_type": card.employment_type or "",
        "salary": card.salary or "",
        "url": card.apply_url or "",
        "source": card.source,
        "applied": FALSE,
        "cv_status": "",
        "first_seen": now,
        "last_seen": now,
    }


def export(config: Config, cards: list[Any], path: pathlib.Path | None = None) -> ExportResult:
    """Upsert the cards into the rolling file."""
    path = path or csv_path(config)
    existing = read(path)
    now = utcnow().date().isoformat()
    result = ExportResult(path=path)

    for card in cards:
        key = str(card.job_id)
        fresh = row_from_card(card, now)
        if key in existing:
            row = existing[key]
            for column in REFRESHED:
                row[column] = fresh[column]
            result.refreshed += 1
        else:
            existing[key] = fresh
            result.added += 1

    _write(path, existing)
    result.total = len(existing)
    return result


def mark_applied(
    path: pathlib.Path, job_ids: list[int], cv_status: str | None = None
) -> tuple[list[int], list[int]]:
    """Flip applied to TRUE. Returns (marked, not_found)."""
    rows = read(path)
    marked: list[int] = []
    missing: list[int] = []
    for job_id in job_ids:
        key = str(job_id)
        if key not in rows:
            missing.append(job_id)
            continue
        rows[key]["applied"] = TRUE
        if cv_status is not None:
            rows[key]["cv_status"] = cv_status
        marked.append(job_id)
    if marked:
        _write(path, rows)
    return marked, missing


def set_applied(path: pathlib.Path, job_id: int, applied: bool) -> bool:
    """Flip one row's applied column either way.

    `mark_applied` only ever sets TRUE, which suits the CLI's one-way command.
    The browser button toggles, so it needs to be able to clear the column too.
    """
    rows = read(path)
    key = str(job_id)
    if key not in rows:
        return False
    rows[key]["applied"] = TRUE if applied else FALSE
    _write(path, rows)
    return True


def set_cv_status(path: pathlib.Path, job_id: int, status: str) -> bool:
    rows = read(path)
    key = str(job_id)
    if key not in rows:
        return False
    rows[key]["cv_status"] = status
    _write(path, rows)
    return True


def _write(path: pathlib.Path, rows: dict[str, dict[str, str]]) -> None:
    """Atomic: write a sibling temp file, then replace.

    A half-written jobs.csv would lose which jobs have been applied to, and that
    is the one thing in this file nothing else can reconstruct.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda row: _sort_key(row))
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    temp.replace(path)


def _sort_key(row: dict[str, str]) -> tuple[int, float, int]:
    """Unapplied first, then by fit descending. The file is read by a human."""
    applied = 1 if row.get("applied") == TRUE else 0
    try:
        fit = float(row.get("fit") or 0.0)
    except ValueError:
        fit = 0.0
    try:
        job_id = int(row.get("job_id") or 0)
    except ValueError:
        job_id = 0
    return (applied, -fit, -job_id)
