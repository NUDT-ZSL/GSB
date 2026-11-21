from __future__ import annotations

from pathlib import Path

import pytest

from mvengine.package import load_package
from mvengine.samplegen import generate_suite


@pytest.fixture(scope="session")
def sample_suite(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("samples")
    paths = generate_suite(root)
    return {name: Path(value) for name, value in paths.items()}


@pytest.fixture(scope="session")
def base_package(sample_suite: dict[str, Path]):
    return load_package(sample_suite["base"])


@pytest.fixture(scope="session")
def append_package(sample_suite: dict[str, Path]):
    return load_package(sample_suite["append"])


@pytest.fixture(scope="session")
def next_package(sample_suite: dict[str, Path]):
    return load_package(sample_suite["next"])


@pytest.fixture(scope="session")
def rollback_package(sample_suite: dict[str, Path]):
    return load_package(sample_suite["rollback"])

