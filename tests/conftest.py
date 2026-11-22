from __future__ import annotations

from pathlib import Path

import pytest

from flightfusion.config import load_default_workspace
from flightfusion.generator import write_samples


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def workspace_dir(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("workspace")
    write_samples(root, force=True)
    return root


@pytest.fixture()
def state(workspace_dir):
    return load_default_workspace(workspace_dir / "data")


@pytest.fixture()
def cache_dir(tmp_path):
    return tmp_path / "cache"

