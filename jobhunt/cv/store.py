"""profile.json: read, write, back up, restore.

The profile is now the single source every master CV and every tailored CV is
cut from, so the rules the master.tex editor kept move here unchanged: every
write takes a timestamped copy first, backups are never rewritten or deleted
here, and a restore is itself a write, so undoing a restore stays possible.
Nothing that fails validation is ever written.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import tempfile

from jobhunt.config import Config
from jobhunt.cv import model

BACKUP_STAMP = "%Y%m%d-%H%M%S"
PROFILE_BACKUP = re.compile(r"^profile-\d{8}-\d{6}(-\d+)?\.json$")


class StoreError(ValueError):
    """Something the user can fix, phrased for them."""


@dataclasses.dataclass(frozen=True)
class Backup:
    name: str
    path: pathlib.Path
    taken_at: dt.datetime
    size: int


@dataclasses.dataclass(frozen=True)
class Saved:
    profile: model.Profile
    path: pathlib.Path
    saved_at: dt.datetime


def master_path(config: Config) -> pathlib.Path:
    return pathlib.Path(str(config.get("tailoring", "master_tex"))).expanduser()


def master_pdf_path(config: Config) -> pathlib.Path:
    # The name the tailoring skill already calls the master's reference render.
    return master_path(config).parent / "Master_CV.pdf"


def backup_dir(config: Config) -> pathlib.Path:
    return master_path(config).parent / "backups"


def profile_path(config: Config) -> pathlib.Path:
    explicit = config.get("tailoring", "profile_json")
    if explicit:
        return pathlib.Path(str(explicit)).expanduser()
    return master_path(config).parent / "profile.json"


def read(config: Config) -> Saved | None:
    """The saved profile, or None before the first save."""
    path = profile_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path} is not valid JSON: {exc.msg} on line {exc.lineno}") from exc
    try:
        profile = model.parse(data)
    except model.ProfileInvalid as exc:
        raise StoreError(f"{path} does not hold a valid profile: {exc.field} {exc.message}") from exc
    return Saved(profile=profile, path=path, saved_at=_mtime(path))


def write(config: Config, profile: model.Profile) -> Backup | None:
    """Back up, then replace the profile."""
    path = profile_path(config)
    backup = take_backup(path, backup_dir(config), "profile")
    body = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    write_atomic(path, body)
    return backup


def backups(config: Config) -> list[Backup]:
    """Newest first. Only profile backups this module wrote."""
    folder = backup_dir(config)
    if not folder.is_dir():
        return []
    found = [_backup_of(path) for path in folder.iterdir() if PROFILE_BACKUP.match(path.name)]
    # By time, then name: two saves in one second differ only by a "-1" suffix,
    # and "-" sorts before "." so names alone would come back in the wrong order.
    return sorted(found, key=lambda backup: (backup.taken_at, backup.name), reverse=True)


def restore(config: Config, name: str) -> Backup | None:
    """Put a backup back, taking a backup of what is there now first."""
    if not PROFILE_BACKUP.match(name):
        raise StoreError(f"{name!r} is not one of the backups")
    source = backup_dir(config) / name
    if not source.is_file():
        raise StoreError(f"no backup named {name}")
    try:
        profile = model.parse(json.loads(source.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, model.ProfileInvalid) as exc:
        raise StoreError(f"{name} does not hold a valid profile") from exc
    return write(config, profile)


def take_backup(source: pathlib.Path, folder: pathlib.Path, prefix: str) -> Backup | None:
    """Copy `source` aside as `<prefix>-<stamp><suffix>`. None when there is nothing yet."""
    if not source.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime(BACKUP_STAMP)
    data = source.read_bytes()
    # Two saves inside the same second must not collide, or one of them is lost.
    # Claimed with an exclusive create, so two at once cannot pick the same name.
    counter = 0
    while True:
        suffix = f"-{counter}" if counter else ""
        target = folder / f"{prefix}-{stamp}{suffix}{source.suffix}"
        try:
            with target.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            counter += 1
            continue
        break
    shutil.copystat(source, target)
    return _backup_of(target)


def write_atomic(path: pathlib.Path, data: str | bytes) -> None:
    """Replace `path` in one step, so a crash mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temp file of its own per writer: a shared "<name>.tmp" would let two
    # saves at once publish each other's half-written file.
    handle, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = pathlib.Path(raw)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _backup_of(path: pathlib.Path) -> Backup:
    return Backup(name=path.name, path=path, taken_at=_mtime(path), size=path.stat().st_size)


def _mtime(path: pathlib.Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
