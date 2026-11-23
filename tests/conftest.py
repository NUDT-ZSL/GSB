from __future__ import annotations

from pathlib import Path

import pytest

from transformer_fusion.generate_data import generate_dataset


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("tf-dataset")
    data_dir = root / "data"
    metadata = generate_dataset(data_dir)
    return Path(data_dir), Path(metadata["manifest"])


@pytest.fixture()
def stores(tmp_path: Path):
    return {
        "cache_dir": tmp_path / "cache",
        "snapshot_dir": tmp_path / "snapshots",
    }
