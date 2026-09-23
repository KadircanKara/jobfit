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
import os
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
    home = cache_dir(config) / template.id
    folder = home / key
    with _GUARD:
        lock = _LOCKS[template.id]
    with lock:
        # A cache entry is a folder that appears whole or not at all, so a
        # server stopped halfway through a thumbnail leaves nothing to trip on.
        if folder.is_dir():
            png = folder / "page1.png"
            cached_png = png.read_bytes() if png.exists() else None
            return Preview(True, (folder / "cv.pdf").read_bytes(), cached_png, "")
        try:
            tex = builds.fill(source, profile, trusted=template.trusted)
        except render.RenderError as exc:
            return Preview(False, b"", None, str(exc))
        built = builds.build(config, tex, engine=template.engine, trusted=template.trusted, runner=runner)
        if not built.ok:
            return Preview(False, b"", None, built.log)
        png = thumbnail(built.pdf)
        # One preview per template: whatever an older profile produced is dropped.
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        staging = pathlib.Path(tempfile.mkdtemp(prefix=".staging-", dir=home))
        (staging / "cv.pdf").write_bytes(built.pdf)
        if png:
            (staging / "page1.png").write_bytes(png)
        os.replace(staging, folder)
        return Preview(True, built.pdf, png, built.log)


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
