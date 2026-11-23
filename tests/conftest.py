from __future__ import annotations

from pathlib import Path

import pytest

from ctr_cvr_joint.engine import EngineConfig, JointCTREngine
from ctr_cvr_joint.models import ThresholdConfig
from ctr_cvr_joint.samples import load_samples_jsonl
from ctr_cvr_joint.specs import load_spec
from ctr_cvr_joint.statistics import load_statistics


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


@pytest.fixture(scope="session")
def fixtures():
    return {
        "samples": load_samples_jsonl(DATA / "requests.jsonl"),
        "spec_v1": load_spec(DATA / "spec_v1.txt"),
        "spec_v2": load_spec(DATA / "spec_v2.json"),
        "spec_v3": load_spec(DATA / "spec_v3.json"),
        "spec_v3_rev2": load_spec(DATA / "spec_v3_rev2.json"),
        "stats_v1": load_statistics(DATA / "stats_v1.json"),
        "stats_v2": load_statistics(DATA / "stats_v2.json"),
    }


def strict_config(**overrides) -> EngineConfig:
    values = {
        "seed": 20261124,
        "threshold": ThresholdConfig(
            policy="strict",
            click_threshold=0.45,
            cvr_threshold=0.40,
            tolerance=0.02,
            stable_digits=12,
        ),
        "joint_click_scale": 0.25,
    }
    values.update(overrides)
    return EngineConfig(**values)


def tolerance_config(**overrides) -> EngineConfig:
    values = {
        "seed": 20261124,
        "threshold": ThresholdConfig(
            policy="tolerance",
            click_threshold=0.45,
            cvr_threshold=0.40,
            tolerance=0.02,
            stable_digits=12,
        ),
        "joint_click_scale": 0.25,
    }
    values.update(overrides)
    return EngineConfig(**values)


@pytest.fixture
def engine(tmp_path):
    return JointCTREngine(tmp_path / "engine", strict_config())

