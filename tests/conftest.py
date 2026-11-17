import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from injection_workbench.config import Config, Thresholds, Budget


@pytest.fixture
def config():
    # 小阈值便于在测试内构造规模切换，预算放宽避免干扰
    return Config(
        thresholds=Thresholds(path_count=5, sink_count=3),
        budget=Budget(concurrency=2, deadline_seconds=10.0, max_paths_per_manifest=10000),
    )


@pytest.fixture
def tmp_dirs(tmp_path):
    samples = tmp_path / "samples"
    reports = tmp_path / "reports"
    checkpoints = tmp_path / "checkpoints"
    for directory in (samples, reports, checkpoints):
        directory.mkdir()
    return samples, reports, checkpoints
