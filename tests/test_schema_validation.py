from __future__ import annotations

import json

import pytest

from formbench.cli import main
from formbench.errors import SchemaError
from formbench.schema import load_task


def _write_task(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_generated_task_is_valid(task):
    assert task.image_width == 960
    assert task.image_height == 640
    assert task.grid.rows == 4
    assert task.grid.cols == 4
    assert len(task.images) == 7


def test_invalid_threshold_and_grid_report_json_pointer(tmp_path):
    task_path = tmp_path / "bad.json"
    _write_task(
        task_path,
        {
            "task_id": "bad",
            "image_width": 960,
            "image_height": 640,
            "detection_threshold": 1.4,
            "confidence_floor": -0.1,
            "chunk_grid": {"rows": 3, "cols": 7},
            "concurrency": 0,
            "memory_budget_bytes": -1,
            "round_logic_budget": 10,
            "state_dir": "state",
            "output_dir": "output",
            "vocabulary": ["A"],
            "images": [],
        },
    )
    with pytest.raises(SchemaError) as exc_info:
        load_task(task_path)
    fields = {issue.field for issue in exc_info.value.issues}
    assert "/detection_threshold" in fields
    assert "/confidence_floor" in fields
    assert "/chunk_grid/rows" in fields
    assert "/chunk_grid/cols" in fields
    assert "/concurrency" in fields
    assert "/memory_budget_bytes" in fields
    assert "/images" in fields


def test_invalid_task_cli_creates_no_outputs(tmp_path):
    task_path = tmp_path / "bad.json"
    _write_task(task_path, {"task_id": "bad"})
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"

    exit_code = main(
        [
            "run",
            "--task",
            str(task_path),
            "--state-dir",
            str(state_dir),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 2
    assert not state_dir.exists()
    assert not output_dir.exists()


def test_bad_image_dimensions_returns_e_image_without_outputs(tmp_path, generated_root):
    import shutil

    from formbench.imaging import write_pgm
    import numpy as np

    bad_image = tmp_path / "bad.pgm"
    write_pgm(bad_image, np.full((32, 48), 240, dtype=np.uint8))
    truth = tmp_path / "truth.json"
    truth.write_text('{"blocks": []}', encoding="utf-8")
    task_path = tmp_path / "task.json"
    shutil.copy(generated_root / "samples" / "task.json", task_path)
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["images"] = [
        {
            "image_id": "bad-dimensions",
            "path": str(bad_image),
            "truth_path": str(truth),
        }
    ]
    task_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--task",
            str(task_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    assert exit_code == 3
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "output").exists()
