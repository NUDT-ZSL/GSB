from __future__ import annotations

import pytest

from formbench.generator import generate_dataset
from formbench.schema import load_task


@pytest.fixture(scope="session")
def generated_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("formbench-samples")
    generate_dataset(root)
    return root


@pytest.fixture(scope="session")
def task(generated_root):
    return load_task(generated_root / "samples" / "task.json")
