"""Previews of the person's own details in a template, built once per pair."""
from __future__ import annotations

import copy

from conftest import LatexRecorder, load_fixture

from jobhunt.cv import model, preview, templates
from jobhunt.cv import store as cvstore


class Recorder:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        if self.code == 0:
            (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return self.code, "Output written on cv.pdf (1 page, 1 bytes)." if self.code == 0 else "! broken"


def save(cfg, **basics):
    data = copy.deepcopy(load_fixture("cv/profile.json"))
    data["basics"].update(basics)
    cvstore.write(cfg, model.parse(data))


def test_before_a_profile_exists_previews_show_the_sample(cfg, cv_source):
    assert preview.profile_for(cfg).basics.name == "Alex Morgan"


def test_a_preview_is_built_once_and_then_served_from_the_cache(cfg, cv_source):
    save(cfg)
    runner = LatexRecorder()

    first = preview.preview(cfg, "classic", runner=runner)
    second = preview.preview(cfg, "classic", runner=runner)

    assert first.ok and second.pdf == first.pdf
    assert len(runner.calls) == 2  # two LaTeX passes, one build


def test_changing_the_profile_builds_it_again(cfg, cv_source):
    save(cfg)
    runner = LatexRecorder()
    preview.preview(cfg, "classic", runner=runner)

    save(cfg, name="Ada King")
    preview.preview(cfg, "classic", runner=runner)

    assert len(runner.calls) == 4


def test_a_failed_preview_says_why_and_is_not_cached(cfg, cv_source):
    save(cfg)
    runner = LatexRecorder(code=1, log="! broken")

    result = preview.preview(cfg, "classic", runner=runner)

    assert not result.ok and "broken" in result.log
    assert not any(preview.cache_dir(cfg).rglob("cv.pdf"))


def test_an_uploaded_template_previews_inside_the_sandbox(cfg, cv_source):
    save(cfg)
    added = templates.add(cfg, "Mine", templates.get(cfg, "classic").text(), engine="lualatex")
    runner = LatexRecorder()

    preview.preview(cfg, added.id, runner=runner)

    assert runner.calls[0][0].endswith("sandbox-exec")


def test_no_thumbnail_without_pdftoppm(monkeypatch):
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)

    assert preview.thumbnail(b"%PDF-1.7") is None


def test_a_half_written_cache_entry_is_never_mistaken_for_a_whole_one(cfg, cv_source):
    save(cfg)
    runner = LatexRecorder()
    preview.preview(cfg, "classic", runner=runner)
    # As if the server died mid-write: a staging folder, never renamed into place.
    home = next(preview.cache_dir(cfg).glob("classic"))
    for entry in list(home.iterdir()):
        entry.rename(home / f".staging-{entry.name}")

    again = preview.preview(cfg, "classic", runner=runner)

    assert again.ok and len(runner.calls) == 4
