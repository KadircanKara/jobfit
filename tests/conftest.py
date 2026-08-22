from __future__ import annotations

import json
import pathlib

import pytest

from jobhunt import config as config_module
from jobhunt.db.session import create_all

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def build_config(tmp_path: pathlib.Path) -> config_module.Config:
    """A config rooted at `tmp_path`, with its database created.

    Split out of the `cfg` fixture so a test that needs a *second*, unrelated
    corpus can have one without reaching into fixture internals.
    """
    raw = config_module._merge(
        config_module.DEFAULT_CONFIG,
        {
            "db_path": str(tmp_path / "jobhunt.db"),
            "data_dir": str(tmp_path / "data"),
            # The packaged default is an absolute path into the real ~/.jobhunt,
            # so anything reaching `rank --emit` under test would overwrite the
            # user's own batch file. Redirected here rather than in each test.
            "ranking": {"batch_path": str(tmp_path / "data/rank/batch.json")},
            "http": {"tier1_delay_seconds": 0.0},
        },
    )
    cfg = config_module.Config(raw=raw, path=tmp_path / "config.yaml")
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    create_all(cfg.db_path)
    return cfg


@pytest.fixture
def cfg(tmp_path: pathlib.Path) -> config_module.Config:
    return build_config(tmp_path)


@pytest.fixture
def cfg_for():
    """Build an extra config at a directory the test chooses."""
    return build_config


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
