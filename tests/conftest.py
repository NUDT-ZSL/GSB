from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tsaudit.generator import generate_sample  # noqa: E402


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    target = tmp_path / "samples"
    generate_sample(target)
    return target


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    target = tmp_path / "state"
    target.mkdir()
    return target
