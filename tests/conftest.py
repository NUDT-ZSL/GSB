from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from surface_stitch.generator import generate_sample


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    generate_sample(path)
    return path
