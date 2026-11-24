from __future__ import annotations

import pytest

from acoustic_alignment.sample_generator import generate_sample_set


@pytest.fixture()
def sample_task_path(tmp_path):
    _root, task_path = generate_sample_set(tmp_path / "samples", seed=380)
    return task_path
