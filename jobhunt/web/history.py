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

# Names the person gave their runs, keyed by run id. Kept beside the run files,
# not inside them: a live run rewrites its own file on every phase change and
# would overwrite a name given meanwhile. The suffix keeps it out of the
# "*.json" globs that list and prune runs.
NAMES_FILE = "names.map"
NAME_MAX = 60


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
    _forget_missing(folder)


def listing(config: Config) -> list[dict[str, Any]]:
    """Newest first, and only what a picker needs to draw a row."""
    folder = directory(config)
    if not folder.is_dir():
        return []
    given = _read_names(folder)
    rows = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:KEEP]:
        body = _read(path)
        if body is None:
            continue
        rows.append({
            "run_id": path.stem,
            "name": given.get(path.stem),
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
    path = _path(config, run_id)
    return _read(path) if path else None


def names(config: Config) -> dict[str, str]:
    return _read_names(directory(config))


def rename(config: Config, run_id: str, name: str) -> str | None:
    """Give a saved run a name, or clear it with a blank one.

    Raises LookupError when the run is not on file and ValueError when the name
    is too long.
    """
    path = _path(config, run_id)
    if path is None or not path.is_file():
        raise LookupError(f"no run {run_id} on file")
    clean = " ".join(name.split())
    if len(clean) > NAME_MAX:
        raise ValueError(f"a run name is at most {NAME_MAX} characters")
    folder = path.parent
    given = _read_names(folder)
    if clean:
        given[run_id] = clean
    else:
        given.pop(run_id, None)
    _write_names(folder, given)
    return clean or None


def delete(config: Config, run_id: str) -> bool:
    """Remove a saved run and its name. False when there was nothing to remove.

    Only the run's own record goes: the jobs it found, the CVs cut for them and
    what was applied to all live elsewhere and stay.
    """
    path = _path(config, run_id)
    if path is None or not path.is_file():
        return False
    path.unlink()
    _forget_missing(path.parent)
    return True


def _path(config: Config, run_id: str) -> pathlib.Path | None:
    # A run id reaches this from a URL, so it may not be a run id at all.
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return None
    return directory(config) / f"{run_id}.json"


def _read_names(folder: pathlib.Path) -> dict[str, str]:
    try:
        body = json.loads((folder / NAMES_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(body, dict):
        return {}
    return {str(key): str(value) for key, value in body.items() if isinstance(value, str)}


def _write_names(folder: pathlib.Path, given: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / NAMES_FILE
    staging = target.with_suffix(".writing")
    staging.write_text(json.dumps(given, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(target)


def _forget_missing(folder: pathlib.Path) -> None:
    """Drop names whose run file is gone, so the names file never outgrows the runs."""
    given = _read_names(folder)
    kept = {run_id: name for run_id, name in given.items() if (folder / f"{run_id}.json").is_file()}
    if kept != given:
        _write_names(folder, kept)


def _read(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A run file written by a process that died mid-write is not worth
        # failing the whole listing over.
        return None
    return body if isinstance(body, dict) else None
