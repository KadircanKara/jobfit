"""The last few runs, kept on disk so they outlive the process.

A run's state has always been in memory only: restart the server and the phase
strip, the rank report and the gate verdicts were gone, even though the corpus
they described was still there. This keeps the last `KEEP` of them as files, so
a finished run can be looked at again and a paused one can be picked back up.

Files, not a table. A run's state is one JSON document that only the browser
reads, it is capped at a handful of entries, and putting it in the corpus schema
would mean a migration for something that is closer to an artifact than to data
about jobs.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from jobhunt.config import Config
from jobhunt.db.models import utcnow

# Enough to answer "what did last night's run do", not so many that the
# directory becomes something to manage.
KEEP = 5

# A run id is its start time. Sortable as a string, readable in a filename, and
# unique enough for one machine running one run at a time.
ID_FORMAT = "%Y%m%d-%H%M%S"


def new_id() -> str:
    return utcnow().strftime(ID_FORMAT)


def directory(config: Config) -> pathlib.Path:
    return pathlib.Path(str(config.data_dir)).expanduser() / "runs"


def save(config: Config, run_id: str, payload: dict[str, Any]) -> pathlib.Path:
    """Write one run's state, then prune to the newest `KEEP`.

    Called on every phase change, so it has to be cheap and it has to be safe
    to call from the run's own thread. The write goes to a temporary file and
    is moved into place: a reader that arrives mid-write sees the previous
    state rather than half of this one.
    """
    folder = directory(config)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{run_id}.json"
    staging = target.with_suffix(".json.writing")
    staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(target)
    _prune(folder)
    return target


def _prune(folder: pathlib.Path) -> None:
    saved = sorted(folder.glob("*.json"), reverse=True)
    for stale in saved[KEEP:]:
        stale.unlink(missing_ok=True)


def listing(config: Config) -> list[dict[str, Any]]:
    """Newest first, and only what a picker needs to draw a row."""
    folder = directory(config)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:KEEP]:
        body = _read(path)
        if body is None:
            continue
        rows.append({
            "run_id": path.stem,
            "started_at": body.get("started_at"),
            "finished_at": body.get("finished_at"),
            "phase": body.get("phase"),
            "outcome": body.get("outcome"),
            "shortlisted": sum(
                1 for row in body.get("results") or [] if not row.get("below_bar")
            ),
            "jobs_total": (body.get("counters") or {}).get("jobs_total", 0),
            "resumable": body.get("phase") == "paused",
        })
    return rows


def load(config: Config, run_id: str) -> dict[str, Any] | None:
    """One run's whole state, or None if it has been pruned away."""
    # A run id reaches this from a URL, so it may not be a run id at all.
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return None
    return _read(directory(config) / f"{run_id}.json")


def _read(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A run file written by a process that died mid-write is not worth
        # failing the whole listing over.
        return None
    return body if isinstance(body, dict) else None
