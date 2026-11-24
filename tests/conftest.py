from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from perm_audit.generator import generate_invalid_sample, generate_sample  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sample") / "deterministic"
    generate_sample(path)
    return path


@pytest.fixture(scope="session")
def invalid_sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("invalid_sample")
    generate_invalid_sample(path)
    return path
