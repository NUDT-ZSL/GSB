from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from account_verify.engine import Engine
from account_verify.samplegen import generate_sample_tree


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_sample_tree(path)
    return path


@pytest.fixture
def truth(sample_dir: Path):
    return json.loads((sample_dir / "truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def imported_engine(sample_dir: Path, tmp_path: Path):
    engine = Engine(tmp_path / "state")
    result = engine.import_samples(sample_dir)
    assert result["imported"]["rejections"] == 0
    yield engine
    engine.close()


@pytest.fixture
def adjudicated_engine(imported_engine: Engine):
    result = imported_engine.adjudicate(batch_size=2)
    assert result["final_batch_watermark"] == result["total_items"]
    return imported_engine
