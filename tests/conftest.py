from __future__ import annotations

from pathlib import Path

import pytest

from ctr_pipeline.generator import generate_dataset


@pytest.fixture(scope="session")
def generated_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("ctr-data")
    generate_dataset(directory, seed=20261121, sample_count=48)
    return directory


@pytest.fixture(scope="session")
def generous_budget() -> tuple[int, int]:
    return 50_000_000, 32 * 1024 * 1024
