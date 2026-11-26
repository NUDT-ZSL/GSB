from __future__ import annotations

from typing import Any

import numpy as np

from .errors import LedgerError
from .model import CSRMatrix


def first_csr_diff(left: CSRMatrix, right: CSRMatrix) -> dict[str, Any] | None:
    if left.nrows != right.nrows or left.ncols != right.ncols:
        return {
            "kind": "shape",
            "row": 0,
            "col": 0,
            "left": [left.nrows, left.ncols],
            "right": [right.nrows, right.ncols],
        }

    for row in range(left.nrows):
        left_start = int(left.row_ptr[row])
        left_end = int(left.row_ptr[row + 1])
        right_start = int(right.row_ptr[row])
        right_end = int(right.row_ptr[row + 1])
        left_cols = left.col_idx[left_start:left_end]
        right_cols = right.col_idx[right_start:right_end]
        if left_cols.size != right_cols.size:
            col = int(left_cols[0]) if left_cols.size else (
                int(right_cols[0]) if right_cols.size else 0
            )
            return {
                "kind": "row_nnz",
                "row": row,
                "col": col,
                "left_length": int(left_cols.size),
                "right_length": int(right_cols.size),
            }

        for position in range(left_cols.size):
            left_col = int(left_cols[position])
            right_col = int(right_cols[position])
            if left_col != right_col:
                return {
                    "kind": "col_idx",
                    "row": row,
                    "position": position,
                    "col": left_col,
                    "col_left": left_col,
                    "col_right": right_col,
                }

            left_value = float(left.values[left_start + position])
            right_value = float(right.values[right_start + position])
            if left_value != right_value:
                return {
                    "kind": "value",
                    "row": row,
                    "position": position,
                    "col": left_col,
                    "value_left": left_value,
                    "value_right": right_value,
                }

            left_ts = int(left.timestamps[left_start + position])
            right_ts = int(right.timestamps[right_start + position])
            if left_ts != right_ts:
                return {
                    "kind": "timestamp",
                    "row": row,
                    "position": position,
                    "col": left_col,
                    "timestamp_left": left_ts,
                    "timestamp_right": right_ts,
                }
    return None


def assert_semantic_equal(
    left: CSRMatrix,
    right: CSRMatrix,
    *,
    code: str = "E_SEMANTIC_DRIFT",
    stage: str = "migrate_semantic",
    context: dict[str, Any] | None = None,
) -> None:
    diff = first_csr_diff(left, right)
    if diff is not None:
        details = {"first_diff": diff}
        if context:
            details.update(context)
        raise LedgerError(
            code,
            "CSR semantic mismatch detected",
            stage=stage,
            details=details,
        )


def float_output_equal(left: np.ndarray, right: np.ndarray) -> dict[str, Any]:
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    if left_array.shape != right_array.shape:
        return {
            "equal": False,
            "reason": "shape",
            "left_shape": list(left_array.shape),
            "right_shape": list(right_array.shape),
        }
    exact = bool(np.array_equal(left_array, right_array))
    max_abs = float(np.max(np.abs(left_array - right_array))) if left_array.size else 0.0
    return {
        "equal": exact,
        "allclose_float64": bool(np.allclose(left_array, right_array, rtol=0.0, atol=0.0)),
        "max_abs_diff": max_abs,
    }
