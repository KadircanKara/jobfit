"""Configuration loading.

Config lives at ~/.jobhunt/config.yaml and is created by `jobhunt init`.
Secrets live in .env and are never written here.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Any

import yaml

HOME_DIR = pathlib.Path(os.environ.get("JOBHUNT_HOME", pathlib.Path.home() / ".jobhunt"))
CONFIG_PATH = HOME_DIR / "config.yaml"

# Answers to PLAN.md section 14, confirmed by the user on 2026-08-20 and by reading
# ~/.claude/skills/tailoring-cv/SKILL.md. The tailoring skill owns the application
# folder layout; jobhunt conforms to it rather than the other way round.
DEFAULT_CONFIG: dict[str, Any] = {
    "db_path": str(HOME_DIR / "jobhunt.db"),
    "data_dir": str(HOME_DIR / "data"),
    "tailoring": {
        # Where the tailoring-cv skill already writes. Folder name is
        # "<Company> - <Position>", spelled as the posting spells them.
        "applications_root": str(pathlib.Path.home() / "Career/Job_Applications/Tailored CVs"),
        "master_tex": str(pathlib.Path.home() / "Career/Job_Applications/CV_Source/master.tex"),
        # handoff prints a ready-to-paste instruction and stops. The skill's own
        # approval gate needs a human at it, so subprocess mode is not the default.
        "mode": "handoff",
        # The skill reads the posting from this filename.
        "jd_filename": "jd.txt",
        # For non-English postings, write the faithful English rendering to jd_filename
        # and keep the original next to it as jd.<lang>.txt.
        "jd_language": "en",
    },
    "digest": {
        "limit": 15,
        "allow_non_english": True,
    },
    "http": {
        "user_agent": "jobhunt/0.2 (personal job search tool; kadircann.kara@gmail.com)",
        "timeout_seconds": 30.0,
        # Seconds between requests to the same source. PLAN.md section 3.
        "tier1_delay_seconds": 1.0,
        "tier5_delay_seconds": 3.0,
    },
    "sync": {
        # Hard cap on board fetches per run. PLAN.md non-negotiable 8.
        "max_boards_per_run": 200,
    },
}


@dataclasses.dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: pathlib.Path

    @property
    def home(self) -> pathlib.Path:
        return self.path.parent

    @property
    def db_path(self) -> pathlib.Path:
        return pathlib.Path(self.raw["db_path"]).expanduser()

    @property
    def data_dir(self) -> pathlib.Path:
        return pathlib.Path(self.raw["data_dir"]).expanduser()

    @property
    def raw_dir(self) -> pathlib.Path:
        return self.data_dir / "raw"

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override onto base. Missing keys in a user config fall back to defaults."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: pathlib.Path | None = None) -> Config:
    """Load config, falling back to defaults for anything absent."""
    path = path or CONFIG_PATH
    user: dict[str, Any] = {}
    if path.exists():
        user = yaml.safe_load(path.read_text()) or {}
    return Config(raw=_merge(DEFAULT_CONFIG, user), path=path)


def write_default(path: pathlib.Path | None = None, force: bool = False) -> pathlib.Path:
    """Write the default config. Never clobbers an existing file unless forced."""
    path = path or CONFIG_PATH
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# jobhunt configuration\n"
        "# Paths here match the existing tailoring-cv skill. Changing applications_root\n"
        "# without changing the skill will split your applications across two trees.\n"
    )
    path.write_text(header + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True))
    return path
