"""Building an uploaded template without trusting it."""
from __future__ import annotations

import pathlib
import shutil

import pytest
from conftest import FIXTURES

from jobhunt.cv import builds, latex, sandbox

DOC = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
REAL = pytest.mark.skipif(
    not sandbox.available() or shutil.which("lualatex") is None,
    reason="needs macOS sandbox-exec and lualatex",
)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return 0, "Output written on cv.pdf (1 page, 1 bytes)."


def test_the_profile_closes_the_network_and_the_home_folder(tmp_path):
    rules = sandbox.profile(tmp_path, deny=[tmp_path / "private"])

    assert "(deny network*)" in rules
    assert f'(deny file-read* file-write* (subpath "{pathlib.Path.home().resolve()}"))' in rules
    assert f'(deny file-read* file-write* (subpath "{(tmp_path / "private").resolve()}"))' in rules
    # The build directory is opened last, so it wins over every deny before it.
    assert rules.splitlines()[-1] == f'(allow file-read* file-write* (subpath "{tmp_path.resolve()}"))'


def test_a_path_with_quotes_cannot_break_out_of_its_rule(tmp_path):
    odd = tmp_path / 'a"b'

    assert '(subpath "' + str(odd.resolve()).replace('"', '\\"') + '")' in sandbox.profile(odd)


def test_a_sandboxed_build_runs_the_engine_inside_sandbox_exec():
    runner = Recorder()

    latex.build(DOC, runner=runner, sandboxed=True)

    argv = runner.calls[0]
    assert argv[0] == str(sandbox.SANDBOX_EXEC) and argv[1] == "-p"
    # With no private cache given, the build still gets a throwaway one.
    assert argv[3] == "/usr/bin/env" and argv[4].startswith("TEXMFVAR=")
    assert argv[5].endswith("lualatex") and "-no-shell-escape" in argv


def test_without_a_sandbox_an_untrusted_build_is_refused_not_run_bare(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: False)

    result = latex.build(DOC, sandboxed=True)

    assert not result.ok and "sandbox" in result.log


@REAL
def test_classic_builds_inside_the_sandbox(cfg):
    preamble = (FIXTURES / "cv" / "master_preamble.tex").read_text(encoding="utf-8")
    document = preamble + "\\begin{document}\nHello\n\\end{document}\n"

    result = latex.build(document, sandboxed=True, cache=builds.sandbox_cache(cfg))

    assert result.ok, result.log


@REAL
def test_a_denied_folder_cannot_be_read_into_the_pdf(tmp_path, cfg):
    secret = tmp_path / "secret.tex"
    secret.write_text("TOPSECRETVALUE", encoding="utf-8")
    probe = f"\\documentclass{{article}}\n\\begin{{document}}\n\\input{{{secret}}}\n\\end{{document}}\n"

    open_build = latex.build(probe)
    closed_build = latex.build(probe, sandboxed=True, deny=[tmp_path], cache=builds.sandbox_cache(cfg))

    assert open_build.ok, "the control build must be able to read it, or the test proves nothing"
    assert not closed_build.ok and b"TOPSECRETVALUE" not in closed_build.pdf


def test_tex_user_folders_are_readable_but_never_writable(tmp_path):
    rules = sandbox.profile(tmp_path, cache=tmp_path / "cache")

    for folder in sandbox.tex_user_dirs():
        assert f'(allow file-read* (subpath "{folder}"))' in rules
        assert f'(allow file-read* file-write* (subpath "{folder}"))' not in rules
    assert f'(allow file-read* file-write* (subpath "{(tmp_path / "cache").resolve()}"))' in rules


def test_a_private_cache_is_handed_to_tex_through_the_environment(tmp_path):
    argv = sandbox.wrap(["lualatex", "cv.tex"], tmp_path, cache=tmp_path / "cache")

    assert argv[3:5] == ["/usr/bin/env", f"TEXMFVAR={(tmp_path / 'cache').resolve()}"]


@REAL
def test_an_untrusted_build_cannot_write_where_trusted_builds_read(tmp_path, cfg):
    target = tmp_path / "planted.sty"
    probe = (
        "\\documentclass{article}\n\\begin{document}\n"
        f"\\directlua{{pcall(function() local f = io.output('{target}'); f:write('x'); f:close() end)}}"
        "x\n\\end{document}\n"
    )

    result = latex.build(probe, sandboxed=True, deny=[tmp_path], cache=builds.sandbox_cache(cfg))

    assert result.ok, result.log
    assert not target.exists()
