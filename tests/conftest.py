from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from diagnostic_workbench.engine import Workbench


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "samples" / "sample.json"


@pytest.fixture()
def sample() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture()
def workbench(state_dir: Path, sample: dict) -> Workbench:
    instance = Workbench(state_dir)
    instance.load({"sample": copy.deepcopy(sample)})
    return instance

