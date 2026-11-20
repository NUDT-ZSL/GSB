#!/usr/bin/env python3
"""Generate the deterministic JSONL task list and feature tensors."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blockops.util import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    npy_bytes,
    sha256_bytes,
)


TASK_SPECS: list[dict[str, Any]] = [
    {
        "job_id": "softmax_2d_f64",
        "op_kind": "softmax",
        "input_shape": [3, 10],
        "block_size": 4,
        "axis": -1,
        "eps": 1e-9,
        "dtype": "float64",
        "seed": 1101,
        "norm_mode": "stable",
    },
    {
        "job_id": "softmax_3d_f32_axis1",
        "op_kind": "softmax",
        "input_shape": [2, 7, 3],
        "block_size": 5,
        "axis": 1,
        "eps": 1e-9,
        "dtype": "float32",
        "seed": 1102,
        "norm_mode": "stable",
    },
    {
        "job_id": "softmax_2d_f64_axis0",
        "op_kind": "softmax",
        "input_shape": [5, 4],
        "block_size": 2,
        "axis": 0,
        "eps": 1e-9,
        "dtype": "float64",
        "seed": 1103,
        "norm_mode": "stable",
    },
    {
        "job_id": "layernorm_2d_f64",
        "op_kind": "layernorm",
        "input_shape": [4, 9],
        "block_size": 4,
        "axis": -1,
        "eps": 1e-5,
        "dtype": "float64",
        "seed": 2101,
        "norm_mode": "population",
    },
    {
        "job_id": "layernorm_3d_f32_axis1",
        "op_kind": "layernorm",
        "input_shape": [2, 8, 4],
        "block_size": 2,
        "axis": 1,
        "eps": 1e-5,
        "dtype": "float32",
        "seed": 2102,
        "norm_mode": "population",
    },
    {
        "job_id": "layernorm_2d_f64_unbiased",
        "op_kind": "layernorm",
        "input_shape": [3, 7],
        "block_size": 3,
        "axis": -1,
        "eps": 1e-5,
        "dtype": "float64",
        "seed": 2103,
        "norm_mode": "unbiased",
    },
]


def generate_tensor(spec: dict[str, Any]) -> np.ndarray:
    rng = np.random.default_rng(int(spec["seed"]))
    shape = tuple(int(item) for item in spec["input_shape"])
    values = np.round(rng.standard_normal(shape), decimals=3)
    axis = int(spec["axis"]) % len(shape)
    if spec["op_kind"] == "softmax":
        moved = np.moveaxis(values, axis, -1).copy()
        rows = moved.reshape(-1, shape[axis])
        second_tie = min(int(spec["block_size"]), shape[axis] - 1)
        if shape[axis] > 1:
            rows[0, 0] = 6.0
            rows[0, second_tie] = 6.0
        values = np.moveaxis(moved, -1, axis)
    dtype = np.dtype(spec["dtype"])
    return np.ascontiguousarray(values, dtype=dtype)


def generate(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tensors_dir = output_dir / "tensors"
    tensors_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []
    task_lines: list[bytes] = []
    for spec in TASK_SPECS:
        tensor = generate_tensor(spec)
        data = npy_bytes(tensor)
        path = tensors_dir / f"{spec['job_id']}.npy"
        atomic_write_bytes(path, data)
        task_lines.append(canonical_json_bytes(spec))
        manifest_entries.append(
            {
                "job_id": spec["job_id"],
                "op_kind": spec["op_kind"],
                "dtype": spec["dtype"],
                "shape": list(tensor.shape),
                "seed": spec["seed"],
                "tensor_sha256": sha256_bytes(data),
                "file": f"tensors/{spec['job_id']}.npy",
            }
        )
    tasks_bytes = b"\n".join(task_lines) + b"\n"
    atomic_write_bytes(output_dir / "tasks.jsonl", tasks_bytes)
    manifest = {
        "schema_version": "1.0",
        "generator": "numpy.random.default_rng + deterministic decimal rounding",
        "tasks_sha256": sha256_bytes(tasks_bytes),
        "tasks": manifest_entries,
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    return {
        "output_dir": str(output_dir),
        "task_count": len(TASK_SPECS),
        "tasks_sha256": manifest["tasks_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "samples"),
        help="directory for tasks.jsonl and tensors/",
    )
    args = parser.parse_args()
    summary = generate(Path(args.output_dir))
    from blockops.util import canonical_json_text

    sys.stdout.write(canonical_json_text(summary) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
