from __future__ import annotations

from pathlib import Path

import pytest

from pose_diag.config import PipelineConfig
from pose_diag.samples import generate_samples


@pytest.fixture(scope="session")
def config() -> PipelineConfig:
    return PipelineConfig.load("configs/default.json")


@pytest.fixture(scope="session")
def samples_root(tmp_path_factory: pytest.TempPathFactory, config: PipelineConfig) -> Path:
    root = tmp_path_factory.mktemp("samples")
    generate_samples(root, config, scene_count=3)
    return root


@pytest.fixture(scope="session")
def manifest_path(samples_root: Path) -> Path:
    return samples_root / "manifest.jsonl"

