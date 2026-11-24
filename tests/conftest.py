from __future__ import annotations

from pathlib import Path

import pytest

from sensitivity_engine.loader import load_dataset
from sensitivity_engine.seed import write_sample_workspace
from sensitivity_engine.service import import_dataset
from sensitivity_engine.store import Store


@pytest.fixture()
def sample_root(tmp_path: Path) -> Path:
    write_sample_workspace(tmp_path)
    return tmp_path


@pytest.fixture()
def dataset(sample_root: Path):
    return load_dataset(sample_root / "data")


@pytest.fixture()
def imported_store(dataset) -> Store:
    store = Store(":memory:")
    import_dataset(store, dataset, concurrency=1, queue_limit=8)
    yield store
    store.close()
