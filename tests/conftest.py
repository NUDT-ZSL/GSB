from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_features.config import load_config  # noqa: E402
from table_features.data import generate_rows  # noqa: E402


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def rows() -> list[dict]:
    return generate_rows(20240517, 24)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "features.json")

