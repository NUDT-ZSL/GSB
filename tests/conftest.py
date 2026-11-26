from pathlib import Path

import pytest

from coord_workbench.engine import Samples
from coord_workbench.seedgen import DEFAULT_SEED, generate_all


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    out = tmp_path / "samples"
    generate_all(out, seed=DEFAULT_SEED)
    return out


@pytest.fixture()
def samples(sample_dir: Path) -> Samples:
    return Samples(sample_dir)


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    out = tmp_path / "run"
    out.mkdir()
    return out
