from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lineage_migrator.engine import Engine
from lineage_migrator.models import Sample


@pytest.fixture()
def sample() -> Sample:
    return Sample.load(PROJECT_ROOT / "samples")


@pytest.fixture()
def replayed_store(tmp_path, sample):
    engine = Engine.bootstrap(tmp_path / "lineage.db", sample)
    engine.replay_all(sample)
    return engine.store


@pytest.fixture()
def fresh_engine(tmp_path, sample):
    def _build(name: str = "case.db"):
        return Engine.bootstrap(tmp_path / name, sample)

    return _build
