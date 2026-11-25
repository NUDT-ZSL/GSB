from __future__ import annotations

import math
from typing import Any

import numpy as np

from .canonical import array_digest, sha256_json
from .errors import Codes, WorkbenchError


SUPPORTED_DTYPES = {"float16": np.float16, "float32": np.float32, "float64": np.float64}
LAYOUT_ALIASES = {
    "row_major": "row_major",
    "row": "row_major",
    "c": "row_major",
    "C": "row_major",
    "col_major": "col_major",
    "column_major": "col_major",
    "col": "col_major",
    "f": "col_major",
    "F": "col_major",
}


def _require(condition: bool, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    if not condition:
        raise WorkbenchError(code, message, details)


def _positive_int(value: Any, field: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), Codes.SCHEMA_INVALID, f"{field} must be an integer", {"field": field, "value": value})
    _require(value > 0, Codes.SCHEMA_INVALID, f"{field} must be positive", {"field": field, "value": value})
    return int(value)


def validate_shapes(op_kind: str, shapes: Any) -> dict[str, list[int]]:
    _require(isinstance(shapes, dict), Codes.SHAPE_INVALID, "matrix_shapes must be an object")
    _require("A" in shapes and "B" in shapes, Codes.SHAPE_INVALID, "matrix_shapes must contain A and B")

    def shape(key: str, dimensions: int) -> list[int]:
        value = shapes.get(key)
        _require(isinstance(value, (list, tuple)), Codes.SHAPE_INVALID, f"matrix_shapes.{key} must be a list")
        _require(len(value) == dimensions, Codes.SHAPE_INVALID, f"matrix_shapes.{key} must have {dimensions} dimensions")
        _require(
            all(isinstance(dim, int) and not isinstance(dim, bool) and dim > 0 for dim in value),
            Codes.SHAPE_INVALID,
            f"matrix_shapes.{key} dimensions must be positive integers",
            {"shape": list(value)},
        )
        return [int(dim) for dim in value]

    if op_kind == "gemm":
        a_shape = shape("A", 2)
        b_shape = shape("B", 2)
        m, k_a = a_shape
        k_b, n = b_shape
        _require(k_a == k_b, Codes.SHAPE_INVALID, "GEMM K dimensions do not match", {"A": a_shape, "B": b_shape})
        if "C" in shapes and shapes["C"] is not None:
            c_shape = shape("C", 2)
            _require(c_shape == [m, n], Codes.SHAPE_INVALID, "C shape must be [M, N]", {"C": c_shape, "expected": [m, n]})
        return {"A": a_shape, "B": b_shape, "C": [m, n]}

    if op_kind == "bmm":
        a_shape = shape("A", 3)
        b_shape = shape("B", 3)
        batch_a, m, k_a = a_shape
        batch_b, k_b, n = b_shape
        _require(batch_a == batch_b, Codes.SHAPE_INVALID, "BMM batch dimensions do not match", {"A": a_shape, "B": b_shape})
        _require(k_a == k_b, Codes.SHAPE_INVALID, "BMM K dimensions do not match", {"A": a_shape, "B": b_shape})
        if "C" in shapes and shapes["C"] is not None:
            c_shape = shape("C", 3)
            _require(c_shape == [batch_a, m, n], Codes.SHAPE_INVALID, "C shape must be [B, M, N]", {"C": c_shape, "expected": [batch_a, m, n]})
        return {"A": a_shape, "B": b_shape, "C": [batch_a, m, n]}

    raise WorkbenchError(Codes.SCHEMA_INVALID, f"unsupported op_kind: {op_kind!r}", {"op_kind": op_kind})


def normalize_job(record: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(record, dict), Codes.SCHEMA_INVALID, "job record must be a JSON object")
    job_id = record.get("job_id")
    _require(isinstance(job_id, str) and job_id.strip(), Codes.SCHEMA_INVALID, "job_id must be a non-empty string")

    op_kind = record.get("op_kind")
    _require(op_kind in {"gemm", "bmm"}, Codes.SCHEMA_INVALID, "op_kind must be gemm or bmm", {"op_kind": op_kind})

    normalized_shapes = validate_shapes(op_kind, record.get("matrix_shapes"))

    dtype = record.get("dtype", "float32")
    _require(isinstance(dtype, str), Codes.DTYPE_UNSUPPORTED, "dtype must be a string", {"dtype": dtype})
    if dtype not in SUPPORTED_DTYPES:
        raise WorkbenchError(
            Codes.DTYPE_UNSUPPORTED,
            f"dtype {dtype!r} is not supported; supported values are {sorted(SUPPORTED_DTYPES)}",
            {"dtype": dtype, "supported": sorted(SUPPORTED_DTYPES)},
        )

    raw_layout = record.get("layout", "row_major")
    _require(isinstance(raw_layout, str), Codes.LAYOUT_INVALID, "layout must be a string", {"layout": raw_layout})
    layout = LAYOUT_ALIASES.get(raw_layout) or LAYOUT_ALIASES.get(raw_layout.lower())
    if layout is None:
        raise WorkbenchError(Codes.LAYOUT_INVALID, f"layout {raw_layout!r} is not supported", {"layout": raw_layout})

    block_size = record.get("block_size", 4)
    if isinstance(block_size, bool) or not isinstance(block_size, int):
        raise WorkbenchError(Codes.BLOCK_INVALID, "block_size must be a positive integer", {"block_size": block_size})
    if block_size <= 0:
        raise WorkbenchError(Codes.BLOCK_INVALID, "block_size must be a positive integer", {"block_size": block_size})

    alpha = record.get("alpha", 1.0)
    beta = record.get("beta", 0.0)
    for name, value in (("alpha", alpha), ("beta", beta)):
        _require(isinstance(value, (int, float)) and not isinstance(value, bool), Codes.SCHEMA_INVALID, f"{name} must be numeric", {name: value})
        _require(math.isfinite(float(value)), Codes.SCHEMA_INVALID, f"{name} must be finite", {name: value})

    seed = record.get("seed", 0)
    _require(isinstance(seed, int) and not isinstance(seed, bool), Codes.SCHEMA_INVALID, "seed must be an integer", {"seed": seed})

    revision = record.get("revision", 1)
    _require(isinstance(revision, int) and not isinstance(revision, bool), Codes.SCHEMA_INVALID, "revision must be an integer", {"revision": revision})
    _require(revision >= 1, Codes.SCHEMA_INVALID, "revision must be >= 1", {"revision": revision})

    content_seed = record.get("content_seed", 0)
    _require(isinstance(content_seed, int) and not isinstance(content_seed, bool), Codes.SCHEMA_INVALID, "content_seed must be an integer", {"content_seed": content_seed})

    tolerance = record.get("tolerance") or {"rtol": 1e-8, "atol": 1e-10}
    _require(isinstance(tolerance, dict), Codes.SCHEMA_INVALID, "tolerance must be an object")
    rtol = float(tolerance.get("rtol", 1e-8))
    atol = float(tolerance.get("atol", 1e-10))
    _require(math.isfinite(rtol) and rtol >= 0, Codes.SCHEMA_INVALID, "tolerance.rtol must be non-negative finite")
    _require(math.isfinite(atol) and atol >= 0, Codes.SCHEMA_INVALID, "tolerance.atol must be non-negative finite")

    return {
        "job_id": job_id,
        "op_kind": op_kind,
        "matrix_shapes": normalized_shapes,
        "dtype": dtype,
        "layout": layout,
        "seed": int(seed),
        "block_size": int(block_size),
        "alpha": float(alpha),
        "beta": float(beta),
        "revision": int(revision),
        "content_seed": int(content_seed),
        "tolerance": {"rtol": rtol, "atol": atol},
    }


def spec_fingerprint(spec: dict[str, Any]) -> str:
    keys = [
        "job_id",
        "op_kind",
        "matrix_shapes",
        "dtype",
        "layout",
        "seed",
        "block_size",
        "alpha",
        "beta",
        "revision",
        "tolerance",
    ]
    return sha256_json({key: spec[key] for key in keys})


def validate_arrays(spec: dict[str, Any], arrays: dict[str, np.ndarray]) -> dict[str, dict[str, Any]]:
    digests: dict[str, dict[str, Any]] = {}
    for role in ("A", "B", "C"):
        array = arrays.get(role)
        if array is None:
            if role == "C" and float(spec.get("beta", 0.0)) == 0.0:
                continue
            raise WorkbenchError(Codes.SHAPE_INVALID, f"missing matrix {role}", {"job_id": spec["job_id"], "role": role})
        arr = np.asarray(array)
        expected = tuple(spec["matrix_shapes"][role])
        if tuple(arr.shape) != expected:
            raise WorkbenchError(
                Codes.SHAPE_INVALID,
                f"matrix_shapes.{role} does not match supplied array dimensions",
                {
                    "reason": "matrix_data_dimension_mismatch",
                    "job_id": spec["job_id"],
                    "role": role,
                    "declared": list(expected),
                    "actual": list(arr.shape),
                },
            )
        if not np.isfinite(arr).all():
            raise WorkbenchError(Codes.SCHEMA_INVALID, f"matrix {role} contains non-finite values", {"role": role})
        digests[role] = array_digest(arr)
    return digests


def check_duplicate_job_ids(records: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for record in records:
        job_id = str(record.get("job_id", "<missing>"))
        if job_id in seen and job_id not in duplicates:
            duplicates.append(job_id)
        seen.add(job_id)
    if duplicates:
        raise WorkbenchError(
            Codes.JOB_DUPLICATE,
            "duplicate job_id values in submission",
            {"duplicate_job_ids": duplicates},
        )


def check_revision_progress(
    normalized_records: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> None:
    regressions: list[dict[str, Any]] = []
    for spec in normalized_records:
        job_id = spec["job_id"]
        old = previous.get(job_id)
        if old and int(spec["revision"]) < int(old["revision"]):
            regressions.append(
                {
                    "job_id": job_id,
                    "submitted_revision": spec["revision"],
                    "committed_revision": old["revision"],
                }
            )
    if regressions:
        raise WorkbenchError(
            Codes.REVISION_REGRESSION,
            "one or more revisions are older than the committed revision",
            {"regressions": regressions},
        )
