from __future__ import annotations

import json
import pathlib

import pytest

from jobhunt import config as config_module
from jobhunt.db.session import create_all

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path: pathlib.Path) -> config_module.Config:
    raw = config_module._merge(
        config_module.DEFAULT_CONFIG,
        {
            "db_path": str(tmp_path / "jobhunt.db"),
            "data_dir": str(tmp_path / "data"),
            "http": {"tier1_delay_seconds": 0.0},
        },
    )
    cfg = config_module.Config(raw=raw, path=tmp_path / "config.yaml")
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    create_all(cfg.db_path)
    return cfg


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
