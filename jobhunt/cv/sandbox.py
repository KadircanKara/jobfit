"""Running a build of an uploaded template without trusting it.

lualatex cannot be confined from the inside: `--safer` breaks luaotfload and
`openin_any=p` stops it from starting (both checked on 2026-09-23). So an uploaded
template builds under macOS's sandbox-exec, with a profile that denies the
network and every read or write under the home folder, except TeX's own
per-user folders and the scratch directory the build runs in. Built-in
templates ship with the package and build without it.
"""
from __future__ import annotations

import functools
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterable

SANDBOX_EXEC = pathlib.Path("/usr/bin/sandbox-exec")


def available() -> bool:
    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def wrap(argv: list[str], workdir: pathlib.Path, deny: Iterable[pathlib.Path] = ()) -> list[str]:
    """`argv` run inside the sandbox. The program is resolved to an absolute path
    first, because sandbox-exec runs it without searching PATH."""
    program = shutil.which(argv[0]) or argv[0]
    return [str(SANDBOX_EXEC), "-p", profile(workdir, deny), program, *argv[1:]]


def profile(workdir: pathlib.Path, deny: Iterable[pathlib.Path] = ()) -> str:
    home = pathlib.Path.home().resolve()
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        f"(deny file-read* file-write* (subpath {_quote(home)}))",
    ]
    rules += [f"(deny file-read* file-write* (subpath {_quote(path.resolve())}))" for path in deny]
    # Later rules win: TeX's per-user folders (font caches, TEXMFHOME) and the
    # build directory are opened back up after everything else is closed.
    rules += [f"(allow file-read* file-write* (subpath {_quote(path)}))" for path in _tex_user_dirs()]
    rules.append(f"(allow file-read* file-write* (subpath {_quote(workdir.resolve())}))")
    return "\n".join(rules)


@functools.lru_cache(maxsize=1)
def _tex_user_dirs() -> tuple[pathlib.Path, ...]:
    found: list[pathlib.Path] = []
    kpsewhich = shutil.which("kpsewhich")
    for variable in ("TEXMFHOME", "TEXMFVAR", "TEXMFCONFIG"):
        if kpsewhich is None:
            break
        done = subprocess.run(
            [kpsewhich, "-var-value", variable], capture_output=True, text=True, check=False, timeout=10
        )
        if done.stdout.strip():
            found.append(pathlib.Path(done.stdout.strip()).expanduser().resolve())
    home = pathlib.Path.home()
    found += [(home / "Library" / "texlive").resolve(), (home / "Library" / "texmf").resolve()]
    return tuple(dict.fromkeys(found))


def _quote(path: pathlib.Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
