"""Building an uploaded template without trusting it."""
from __future__ import annotations

import pathlib
import shutil

import pytest
from conftest import FIXTURES, LatexRecorder

from jobhunt.cv import latex, sandbox

DOC = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
REAL = pytest.mark.skipif(
    not sandbox.available() or shutil.which("lualatex") is None,
    reason="needs macOS sandbox-exec and lualatex",
)


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
    runner = LatexRecorder()

    latex.build(DOC, runner=runner, sandboxed=True)

    argv = runner.calls[0]
    assert argv[0] == str(sandbox.SANDBOX_EXEC) and argv[1] == "-p"
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

    result = latex.build(document, sandboxed=True, seed=sandbox.texmf_var())

    assert result.ok, result.log


@REAL
def test_a_denied_folder_cannot_be_read_into_the_pdf(tmp_path, cfg):
    secret = tmp_path / "secret.tex"
    secret.write_text("TOPSECRETVALUE", encoding="utf-8")
    probe = f"\\documentclass{{article}}\n\\begin{{document}}\n\\input{{{secret}}}\n\\end{{document}}\n"

    open_build = latex.build(probe)
    closed_build = latex.build(probe, sandboxed=True, deny=[tmp_path], seed=sandbox.texmf_var())

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

    result = latex.build(probe, sandboxed=True, deny=[tmp_path], seed=sandbox.texmf_var())

    assert result.ok, result.log
    assert not target.exists()


def test_each_build_gets_its_own_copy_of_the_font_cache(tmp_path):
    seed = tmp_path / "seed"
    (seed / "luatex-cache").mkdir(parents=True)
    (seed / "luatex-cache" / "names.luc").write_text("fonts", encoding="utf-8")
    seen = []

    def plants(argv, cwd):
        cache = pathlib.Path(next(arg for arg in argv if arg.startswith("TEXMFVAR="))[len("TEXMFVAR="):])
        seen.append((cache, (cache / "luatex-cache" / "names.luc").exists()))
        (cache / "tex" / "latex").mkdir(parents=True, exist_ok=True)
        (cache / "tex" / "latex" / "article.cls").write_text("planted", encoding="utf-8")
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return 0, "Output written on cv.pdf (1 page, 13 bytes)."

    latex.build(DOC, runner=plants, sandboxed=True, seed=seed)
    latex.build(DOC, runner=plants, sandboxed=True, seed=seed)

    # Two passes per build, so the first entry is build one and the last is build two.
    (first, copied), (second, _) = seen[0], seen[-1]
    assert copied, "the build starts from the seeded fonts"
    assert first != second and not first.exists(), "what one build wrote is gone before the next"
    assert not (seed / "tex").exists(), "the seed is never written"


@REAL
def test_a_file_one_build_plants_is_not_found_by_the_next(tmp_path):
    look = "\\IfFileExists{poc.sty}{\\typeout{PLANTED-FOUND}}{}"
    plant = (
        "\\documentclass{article}\n"
        "\\directlua{local v = os.getenv('TEXMFVAR'); lfs.mkdir(v .. '/tex'); lfs.mkdir(v .. '/tex/latex');"
        " local f = io.open(v .. '/tex/latex/poc.sty', 'w'); f:write('x'); f:close()}\n"
        + look + "\n\\begin{document}\nx\n\\end{document}\n"
    )
    probe = "\\documentclass{article}\n" + look + "\n\\begin{document}\nx\n\\end{document}\n"

    planted = latex.build(plant, sandboxed=True, deny=[tmp_path], seed=sandbox.texmf_var())
    after = latex.build(probe, sandboxed=True, deny=[tmp_path], seed=sandbox.texmf_var())

    # The planting build's own second pass finds it, or this test proves nothing.
    assert planted.ok and "PLANTED-FOUND" in planted.log, planted.log
    assert after.ok and "PLANTED-FOUND" not in after.log
