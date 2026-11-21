from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np

from bmm_pipeline.jobs import JobSpec, validate_job


BASE_JOB: dict[str, Any] = {
    "job_id": "job-a",
    "op_kind": "gemm",
    "matrix_shapes": [[9, 7], [7, 8]],
    "dtype": "float64",
    "layout": "row",
    "seed": 42,
    "block_size": 4,
    "accum_mode": "kahan",
}


def base_job(**overrides: Any) -> dict[str, Any]:
    value = copy.deepcopy(BASE_JOB)
    value.update(overrides)
    return value


def make_job(**overrides: Any) -> JobSpec:
    return validate_job(base_job(**overrides))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def load_output_arrays(run_dir: Path) -> dict[str, np.ndarray]:
    summary = json.loads(
        (run_dir / "final_results.json").read_text(encoding="utf-8")
    )
    return {
        item["job_id"]: np.load(
            run_dir / "results" / item["output_file"], allow_pickle=False
        )
        for item in summary["jobs"]
    }
