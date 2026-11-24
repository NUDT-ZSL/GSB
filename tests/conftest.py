from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smuggle_detector.samplegen import generate_sample  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("deterministic-samples")
    generate_sample(path, seed=1337)
    return path


@pytest.fixture(scope="session")
def truth(sample_dir: Path) -> dict:
    from smuggle_detector.io_utils import read_json

    return read_json(sample_dir / "truth.json")

