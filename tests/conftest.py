from __future__ import annotations

from pathlib import Path

import pytest

from telemetry_drift.samplegen import generate_dataset


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("fixed-seed-data")
    generate_dataset(path)
    return path

