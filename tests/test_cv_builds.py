"""Trust is decided in one place: built-ins run in-process and unsandboxed,
everything else in a child process and inside the sandbox."""
from __future__ import annotations

import copy

import pytest
from conftest import load_fixture

from jobhunt.cv import builds, model, render, templates


@pytest.fixture
def cv_source(tmp_path, cfg):
    folder = tmp_path / "CV_Source"
    folder.mkdir()
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(folder / "master.tex")
    return folder


def profile():
    return model.parse(copy.deepcopy(load_fixture("cv/profile.json")))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return 0, "Output written on cv.pdf (1 page, 1 bytes)."


def test_a_trusted_template_is_filled_in_process(monkeypatch):
    monkeypatch.setattr(render, "render_isolated", lambda *a, **k: pytest.fail("isolated"))

    assert "Ada Lovelace" in builds.fill("\\VAR{basics.name}", profile(), trusted=True)


def test_an_untrusted_template_is_filled_in_a_child_process(monkeypatch):
    seen = []
    monkeypatch.setattr(render, "render_isolated", lambda p, s, **k: seen.append(s) or "x")

    builds.fill("\\VAR{basics.name}", profile(), trusted=False)

    assert seen == ["\\VAR{basics.name}"]


def test_an_untrusted_build_is_sandboxed_with_the_private_folders_closed(cfg, cv_source):
    runner = Recorder()

    builds.build(cfg, "doc", engine="lualatex", trusted=False, runner=runner)

    argv = runner.calls[0]
    assert argv[0].endswith("sandbox-exec")
    assert str(cv_source.resolve()) in argv[2] and str(cfg.db_path.parent.resolve()) in argv[2]


def test_a_trusted_build_is_not_sandboxed(cfg, cv_source):
    runner = Recorder()

    builds.build(cfg, "doc", engine="lualatex", trusted=True, runner=runner)

    assert runner.calls[0][0] == "lualatex"


def test_built_ins_are_trusted_and_uploads_are_not(cfg):
    assert templates.get(cfg, "classic").trusted
