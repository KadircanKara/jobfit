"""A template with the person's own details in it: the gallery's thumbnails
and full previews.

Each is built once per (template, profile) pair and kept until either one
changes, because a gallery page asks for every template at once. Before a
profile exists, previews show the made-up sample person instead.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
import threading

from jobhunt.config import Config
from jobhunt.cv import builds, latex, model, render, templates
from jobhunt.cv import store as cvstore

_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)


@dataclasses.dataclass(frozen=True)
class Preview:
    ok: bool
    pdf: bytes
    png: bytes | None
    log: str
    missing: tuple[str, ...] = ()


def cache_dir(config: Config) -> pathlib.Path:
    return pathlib.Path(config.db_path).parent / "cv_cache"


def profile_for(config: Config) -> model.Profile:
    saved = cvstore.read(config)
    return saved.profile if saved else templates.sample_profile()


def preview(config: Config, template_id: str, *, runner: latex.Runner | None = None) -> Preview:
    template = templates.get(config, template_id)
    profile = profile_for(config)
    source = template.text()
    key = hashlib.sha256(f"{source}\0{template.engine}\0{profile.fingerprint()}".encode()).hexdigest()[:16]
    folder = cache_dir(config) / template.id / key
    with _GUARD:
        lock = _LOCKS[template.id]
    with lock:
        if (folder / "cv.pdf").exists():
            png = folder / "page1.png"
            return Preview(
                True,
                (folder / "cv.pdf").read_bytes(),
                png.read_bytes() if png.exists() else None,
                "",
                tuple(json.loads((folder / "missing.json").read_text(encoding="utf-8"))),
            )
        try:
            tex = builds.fill(source, profile, trusted=template.trusted)
        except render.RenderError as exc:
            return Preview(False, b"", None, str(exc))
        built = builds.build(config, tex, engine=template.engine, trusted=template.trusted, runner=runner)
        if not built.ok:
            return Preview(False, b"", None, built.log)
        # One preview per template: whatever an older profile produced is dropped.
        shutil.rmtree(cache_dir(config) / template.id, ignore_errors=True)
        folder.mkdir(parents=True)
        (folder / "cv.pdf").write_bytes(built.pdf)
        png = thumbnail(built.pdf)
        if png:
            (folder / "page1.png").write_bytes(png)
        (folder / "missing.json").write_text(json.dumps(list(built.missing)), encoding="utf-8")
        return Preview(True, built.pdf, png, built.log, built.missing)


def thumbnail(pdf: bytes) -> bytes | None:
    """Page one as a PNG, or None where poppler's pdftoppm is not installed."""
    binary = shutil.which("pdftoppm")
    if binary is None:
        return None
    with tempfile.TemporaryDirectory(prefix="jobhunt-thumb-") as raw:
        folder = pathlib.Path(raw)
        (folder / "in.pdf").write_bytes(pdf)
        done = subprocess.run(
            [binary, "-png", "-r", "60", "-f", "1", "-l", "1", "-singlefile",
             str(folder / "in.pdf"), str(folder / "page")],
            capture_output=True, timeout=60, check=False,
        )
        out = folder / "page.png"
        return out.read_bytes() if done.returncode == 0 and out.exists() else None
