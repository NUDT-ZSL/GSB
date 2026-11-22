"""Deterministic local CSR sample and write-batch generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .batches import seal_batch


def _random_csr(
    rng: np.random.Generator,
    rows: int,
    cols: int,
    *,
    min_row_nnz: int = 1,
    max_row_nnz: int = 4,
) -> dict[str, Any]:
    indptr = [0]
    indices: list[int] = []
    values: list[float] = []
    upper = min(cols, max_row_nnz)
    lower = min(min_row_nnz, upper)
    for _ in range(rows):
        count = int(rng.integers(lower, upper + 1))
        selected = np.sort(rng.choice(cols, size=count, replace=False))
        for col in selected:
            value = 0.0
            while value == 0.0:
                value = round(float(rng.normal(0.0, 1.0)), 6)
            indices.append(int(col))
            values.append(value)
        indptr.append(len(indices))
    return {"indptr": indptr, "indices": indices, "values": values}


def _row_content(
    rng: np.random.Generator,
    row: int,
    cols: int,
    *,
    min_nnz: int = 1,
    max_nnz: int = 4,
) -> dict[str, Any]:
    upper = min(cols, max_nnz)
    lower = min(min_nnz, upper)
    count = int(rng.integers(lower, upper + 1))
    selected = np.sort(rng.choice(cols, size=count, replace=False))
    values: list[float] = []
    for _ in selected:
        value = 0.0
        while value == 0.0:
            value = round(float(rng.normal(0.0, 1.0)), 6)
        values.append(value)
    return {
        "row": row,
        "col_indices": [int(item) for item in selected],
        "values": values,
    }


def _replace_batch(
    *,
    rng: np.random.Generator,
    batch_id: str,
    version: int,
    row_start: int,
    row_end: int,
    cols: int,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "batch_id": batch_id,
        "write_version": version,
        "row_start": row_start,
        "row_end": row_end,
        "mode": "replace",
        "rows": rows
        if rows is not None
        else [
            _row_content(rng, row, cols)
            for row in range(row_start, row_end)
        ],
    }
    return seal_batch(payload)


def _invalidate_batch(
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


def _spmm_case(seed: int) -> dict[str, Any]:
    case_id = "spmm_inc_demo"
    m, n, k = 20, 7, 12
    rng = np.random.default_rng(seed)
    a = _random_csr(rng, m, k)
    b = _random_csr(rng, k, n, min_row_nnz=1, max_row_nnz=3)
    writes_rng = np.random.default_rng(seed + 1)
    first = _replace_batch(
        rng=writes_rng,
        batch_id="spmm_w01",
        version=1,
        row_start=0,
        row_end=4,
        cols=k,
    )
    duplicate_rows = [dict(row) for row in first["rows"] if int(row["row"]) in {0, 1}]
    writes = [
        first,
        _replace_batch(
            rng=writes_rng,
            batch_id="spmm_w02",
            version=2,
            row_start=3,
            row_end=7,
            cols=k,
        ),
        _invalidate_batch(
            batch_id="spmm_w03",
            version=3,
            row_start=6,
            row_end=8,
        ),
        _replace_batch(
            rng=writes_rng,
            batch_id="spmm_w04",
            version=4,
            row_start=12,
            row_end=16,
            cols=k,
        ),
        _replace_batch(
            rng=writes_rng,
            batch_id="spmm_w05",
            version=5,
            row_start=0,
            row_end=2,
            cols=k,
            rows=duplicate_rows,
        ),
    ]
    return {
        "case_id": case_id,
        "operation": "spmm",
        "shape_mnk": [m, n, k],
        "nnz_a": len(a["values"]),
        "nnz_b": len(b["values"]),
        "dtype": "float64",
        "seed": seed,
        "base_csr": {"csr_a": a, "csr_b": b},
        "write_batches": writes,
    }


def _spspmm_case(seed: int) -> dict[str, Any]:
    case_id = "spspmm_inc_demo"
    m, n, k = 18, 9, 14
    rng = np.random.default_rng(seed)
    a = _random_csr(rng, m, k)
    b = _random_csr(rng, k, n, min_row_nnz=1, max_row_nnz=4)
    writes_rng = np.random.default_rng(seed + 1)
    first = _replace_batch(
        rng=writes_rng,
        batch_id="spspmm_w01",
        version=1,
        row_start=0,
        row_end=4,
        cols=k,
    )
    duplicate_rows = [dict(row) for row in first["rows"] if int(row["row"]) in {0, 1}]
    writes = [
        first,
        _replace_batch(
            rng=writes_rng,
            batch_id="spspmm_w02",
            version=2,
            row_start=3,
            row_end=7,
            cols=k,
        ),
        _invalidate_batch(
            batch_id="spspmm_w03",
            version=3,
            row_start=12,
            row_end=16,
        ),
        _replace_batch(
            rng=writes_rng,
            batch_id="spspmm_w04",
            version=4,
            row_start=0,
            row_end=2,
            cols=k,
            rows=duplicate_rows,
        ),
    ]
    return {
        "case_id": case_id,
        "operation": "spspmm",
        "shape_mnk": [m, n, k],
        "nnz_a": len(a["values"]),
        "nnz_b": len(b["values"]),
        "dtype": "float64",
        "seed": seed,
        "base_csr": {"csr_a": a, "csr_b": b},
        "write_batches": writes,
    }


def generate_samples(output_dir: str | Path, *, seed: int = 20261123) -> dict[str, str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases = [_spmm_case(seed), _spspmm_case(seed + 101)]
    manifest_path = output / "cases.jsonl"
    with open(manifest_path, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(
                json.dumps(case, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )
    writes_path = output / "write_batches.jsonl"
    with open(writes_path, "w", encoding="utf-8") as handle:
        for case in cases:
            for batch in case["write_batches"]:
                handle.write(
                    json.dumps(
                        {
                            "case_id": case["case_id"],
                            "batch": batch,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )
    metadata_path = output / "sample_metadata.json"
    metadata = {
        "schema_version": "sparse-inc-samples/v1",
        "seed": seed,
        "cases": [
            {
                "case_id": case["case_id"],
                "operation": case["operation"],
                "shape_mnk": case["shape_mnk"],
                "nnz_a": case["nnz_a"],
                "nnz_b": case["nnz_b"],
                "write_batches": len(case["write_batches"]),
            }
            for case in cases
        ],
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "manifest": str(manifest_path),
        "write_batches": str(writes_path),
        "metadata": str(metadata_path),
    }
