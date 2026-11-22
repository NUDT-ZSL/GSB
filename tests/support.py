"""Shared deterministic fixtures for unit tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from sparse_inc.batches import seal_batch
from sparse_inc.case import SampleCase, load_cases
from sparse_inc.samples import generate_samples


def make_sample_directory() -> tuple[tempfile.TemporaryDirectory[str], dict[str, SampleCase]]:
    temp = tempfile.TemporaryDirectory(prefix="sparse-inc-test-")
    paths = generate_samples(Path(temp.name) / "samples")
    return temp, load_cases(paths["manifest"])


def raw_case(case: SampleCase) -> dict[str, Any]:
    return json.loads(json.dumps(case.raw))


def replace_batch(
    *,
    batch_id: str,
    version: int,
    row_start: int,
    row_end: int,
    rows: list[dict[str, Any]] | None = None,
    col: int = 0,
    value: float = 1.0,
) -> dict[str, Any]:
    if rows is None:
        rows = [
            {
                "row": row,
                "col_indices": [col],
                "values": [value + row * 0.125],
            }
            for row in range(row_start, row_end)
        ]
    return seal_batch(
        {
            "batch_id": batch_id,
            "write_version": version,
            "row_start": row_start,
            "row_end": row_end,
            "mode": "replace",
            "rows": rows,
        }
    )


def invalidate_batch(
    *,
    batch_id: str,
    version: int,
    row_start: int,
    row_end: int,
) -> dict[str, Any]:
    return seal_batch(
        {
            "batch_id": batch_id,
            "write_version": version,
            "row_start": row_start,
            "row_end": row_end,
            "mode": "invalidate",
            "rows": [],
        }
    )
