"""Running a build of an uploaded template without trusting it.

lualatex cannot be confined from the inside: `--safer` breaks luaotfload and
`openin_any=p` stops it from starting (both checked on 2026-09-23). So an uploaded
template builds under macOS's sandbox-exec, with a profile that denies the
network and every read or write under the home folder. TeX's own per-user
folders are readable but never writable, the font cache it needs to write is a
private copy, and only the scratch directory the build runs in is writable.
Built-in templates ship with the package and build without it.
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


def wrap(
    argv: list[str],
    workdir: pathlib.Path,
    deny: Iterable[pathlib.Path] = (),
    cache: pathlib.Path | None = None,
) -> list[str]:
    """`argv` run inside the sandbox. The program is resolved to an absolute path
    first, because sandbox-exec runs it without searching PATH. With `cache`, TeX
    keeps its font cache there instead of in the shared per-user folder."""
    program = shutil.which(argv[0]) or argv[0]
    command = [program, *argv[1:]]
    if cache is not None:
        command = ["/usr/bin/env", f"TEXMFVAR={cache.resolve()}", *command]
    return [str(SANDBOX_EXEC), "-p", profile(workdir, deny, cache), *command]


def profile(
    workdir: pathlib.Path, deny: Iterable[pathlib.Path] = (), cache: pathlib.Path | None = None
) -> str:
    home = pathlib.Path.home().resolve()
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        f"(deny file-read* file-write* (subpath {_quote(home)}))",
    ]
    rules += [f"(deny file-read* file-write* (subpath {_quote(path.resolve())}))" for path in deny]
    # Later rules win. TeX's own per-user folders are opened for reading only:
    # the built-in templates build outside the sandbox and load from them, so a
    # file an upload wrote there would run unconfined on the next Classic build.
    rules += [f"(allow file-read* (subpath {_quote(path)}))" for path in tex_user_dirs()]
    if cache is not None:
        rules.append(f"(allow file-read* file-write* (subpath {_quote(cache.resolve())}))")
    rules.append(f"(allow file-read* file-write* (subpath {_quote(workdir.resolve())}))")
    return "\n".join(rules)


def texmf_var() -> pathlib.Path | None:
    """The shared font-cache folder, which an upload's private cache is seeded from."""
    value = _kpse("TEXMFVAR")
    return pathlib.Path(value).expanduser().resolve() if value else None


@functools.lru_cache(maxsize=1)
def tex_user_dirs() -> tuple[pathlib.Path, ...]:
    found = [
        pathlib.Path(value).expanduser().resolve()
        for value in (_kpse(variable) for variable in ("TEXMFHOME", "TEXMFVAR", "TEXMFCONFIG"))
        if value
    ]
    home = pathlib.Path.home()
    found += [(home / "Library" / "texlive").resolve(), (home / "Library" / "texmf").resolve()]
    return tuple(dict.fromkeys(found))


@functools.lru_cache(maxsize=8)
def _kpse(variable: str) -> str:
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return ""
    done = subprocess.run(
        [kpsewhich, "-var-value", variable], capture_output=True, text=True, check=False, timeout=10
    )
    return done.stdout.strip()


def _quote(path: pathlib.Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
