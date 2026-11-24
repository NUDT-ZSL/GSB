from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from certpath.engine import CertPathEngine
from certpath.generator import generate_sample


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("certpath-sample") / "samples"
    generate_sample(target)
    return target


@pytest.fixture
def sample_copy(tmp_path: Path, sample_dir: Path) -> Path:
    target = tmp_path / "sample"
    shutil.copytree(sample_dir, target)
    return target


@pytest.fixture
def engine(tmp_path: Path) -> CertPathEngine:
    return CertPathEngine(tmp_path / "state")
