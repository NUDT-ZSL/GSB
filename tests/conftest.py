from __future__ import annotations

from pathlib import Path

import pytest

from latent_diag.config import load_config
from latent_diag.generator import generate_samples
from latent_diag.task import load_manifest


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("samples")
    generate_samples(out)
    return out


@pytest.fixture(scope="session")
def sample_config(sample_dir: Path):
    return load_config(sample_dir / "config.json")


@pytest.fixture(scope="session")
def sample_tasks(sample_dir: Path):
    return load_manifest(sample_dir / "tasks.jsonl")

