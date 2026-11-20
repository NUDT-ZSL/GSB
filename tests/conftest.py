from __future__ import annotations

from pathlib import Path

import pytest

from rope_attention.data import (
    load_tasks,
    load_tensors,
    prepare_sample_workspace,
)


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    prepare_sample_workspace(tmp_path)
    return tmp_path


@pytest.fixture
def sample_inputs(sample_root: Path):
    tasks = load_tasks(sample_root / "samples" / "tasks.jsonl")
    tensors = {
        task.case_id: load_tensors(
            sample_root / "samples" / "tensors" / f"{task.case_id}.npz", task
        )
        for task in tasks
    }
    return tasks, tensors

