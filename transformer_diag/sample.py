from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import atomic_write_text


SAMPLE_BATCH_1: list[dict[str, Any]] = [
    {
        "job_id": "softmax_b1",
        "op_kind": "softmax",
        "input_shape": [4, 16],
        "block_size": 4,
        "axis": -1,
        "eps": 1e-6,
        "dtype": "float32",
        "seed": 20260101,
        "batch_no": 1,
        "revision": 1,
    },
    {
        "job_id": "layernorm_b1",
        "op_kind": "layernorm",
        "input_shape": [3, 5, 12],
        "block_size": 5,
        "axis": 2,
        "eps": 1e-5,
        "dtype": "float32",
        "seed": 20260102,
        "batch_no": 1,
        "revision": 1,
    },
]

SAMPLE_BATCH_2: list[dict[str, Any]] = [
    {
        "job_id": "softmax_b2",
        "op_kind": "softmax",
        "input_shape": [2, 3, 10],
        "block_size": 3,
        "axis": 2,
        "eps": 1e-6,
        "dtype": "float64",
        "seed": 20260103,
        "batch_no": 2,
        "revision": 1,
    },
    {
        "job_id": "layernorm_b2",
        "op_kind": "layernorm",
        "input_shape": [2, 8],
        "block_size": 4,
        "axis": -1,
        "eps": 1e-5,
        "dtype": "float64",
        "seed": 20260104,
        "batch_no": 2,
        "revision": 1,
    },
]


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, sort_keys=True) + "\n" for item in records)


def write_sample_files(directory: str | Path) -> dict[str, str]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    batch1 = target / "batch_1.jsonl"
    batch2 = target / "batch_2.jsonl"
    full = target / "jobs_full.jsonl"
    revision = target / "batch_2_revision.jsonl"

    revision_records = [dict(item) for item in SAMPLE_BATCH_2]
    revision_records[0].update(
        {"revision": 2, "block_size": 5, "eps": 2e-5, "dtype": "float64"}
    )

    atomic_write_text(batch1, _jsonl(SAMPLE_BATCH_1))
    atomic_write_text(batch2, _jsonl(SAMPLE_BATCH_2))
    atomic_write_text(full, _jsonl(SAMPLE_BATCH_1 + SAMPLE_BATCH_2))
    atomic_write_text(revision, _jsonl(revision_records))
    return {
        "batch_1": str(batch1),
        "batch_2": str(batch2),
        "jobs_full": str(full),
        "batch_2_revision": str(revision),
    }


def append_batch(existing: str | Path, batch: str | Path) -> str:
    target = Path(existing)
    source = Path(batch)
    if not source.exists():
        raise BusinessError("E_INPUT_INVALID", f"batch file not found: {source}", field=str(source))
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write_text(target, text + source.read_text(encoding="utf-8"))
    return str(target)

