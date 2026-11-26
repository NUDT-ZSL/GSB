from __future__ import annotations

from typing import Any

import numpy as np

from .errors import LedgerError
from .model import CSRMatrix


def validate_csr(
    matrix: CSRMatrix,
    *,
    stage: str = "parse_validate",
    allow_duplicate_columns: bool = False,
) -> None:
    """Validate canonical CSR invariants.

    Duplicate columns are structural violations for canonical ingestion.
    Format readers may classify duplicates as the more specific E_INDEX_DUP
    before calling this function.
    """

    if matrix.row_ptr.ndim != 1 or matrix.col_idx.ndim != 1:
        raise LedgerError(
            "E_STRUCT",
            "row_ptr and col_idx must be one-dimensional",
            stage=stage,
        )
    if matrix.row_ptr.size != matrix.nrows + 1:
        raise LedgerError(
            "E_STRUCT",
            "row_ptr length must equal nrows + 1",
            stage=stage,
            details={
                "nrows": matrix.nrows,
                "row_ptr_length": int(matrix.row_ptr.size),
            },
        )
    if matrix.row_ptr[0] != 0:
        raise LedgerError(
            "E_STRUCT",
            "row_ptr[0] must be zero",
            stage=stage,
            details={"row_ptr0": int(matrix.row_ptr[0])},
        )
    if np.any(np.diff(matrix.row_ptr) < 0):
        bad = int(np.flatnonzero(np.diff(matrix.row_ptr) < 0)[0])
        raise LedgerError(
            "E_STRUCT",
            "row pointer array is not monotone non-decreasing",
            stage=stage,
            details={
                "at_pointer_index": bad + 1,
                "row_ptr": matrix.row_ptr.tolist(),
            },
        )
    nnz = int(matrix.row_ptr[-1])
    if nnz < 0 or nnz != matrix.col_idx.size:
        raise LedgerError(
            "E_STRUCT",
            "NNZ implied by row_ptr does not match column index length",
            stage=stage,
            details={"row_ptr_nnz": nnz, "col_idx_length": int(matrix.col_idx.size)},
        )
    if matrix.values.size != nnz:
        raise LedgerError(
            "E_STRUCT",
            "NNZ does not match values array length",
            stage=stage,
            details={"nnz": nnz, "values_length": int(matrix.values.size)},
        )
    if matrix.timestamps.size != nnz:
        raise LedgerError(
            "E_STRUCT",
            "NNZ does not match timestamps array length",
            stage=stage,
            details={"nnz": nnz, "timestamps_length": int(matrix.timestamps.size)},
        )
    if nnz > 0 and (matrix.col_idx.min() < 0 or matrix.col_idx.max() >= matrix.ncols):
        bad_index = int(
            np.flatnonzero((matrix.col_idx < 0) | (matrix.col_idx >= matrix.ncols))[0]
        )
        raise LedgerError(
            "E_STRUCT",
            "column index is out of bounds",
            stage=stage,
            details={
                "entry_index": bad_index,
                "column": int(matrix.col_idx[bad_index]),
                "ncols": matrix.ncols,
            },
        )
    if not np.all(np.isfinite(matrix.values)):
        bad_index = int(np.flatnonzero(~np.isfinite(matrix.values))[0])
        raise LedgerError(
            "E_STRUCT",
            "matrix values must be finite float64 numbers",
            stage=stage,
            details={"entry_index": bad_index},
        )

    for row in range(matrix.nrows):
        start = int(matrix.row_ptr[row])
        end = int(matrix.row_ptr[row + 1])
        columns = matrix.col_idx[start:end]
        if columns.size > 1 and np.any(np.diff(columns) <= 0):
            duplicate_or_unsorted = int(np.flatnonzero(np.diff(columns) <= 0)[0])
            entry = start + duplicate_or_unsorted + 1
            code = "E_INDEX_DUP" if columns[duplicate_or_unsorted] == columns[duplicate_or_unsorted + 1] and not allow_duplicate_columns else "E_STRUCT"
            raise LedgerError(
                code,
                "each row must have strictly ascending unique column indices",
                stage=stage,
                details={
                    "row": row,
                    "entry_index": entry,
                    "column": int(columns[duplicate_or_unsorted + 1]),
                    "col_idx": columns.tolist(),
                },
            )


def ingest_dict(data: dict[str, Any], *, stage: str = "struct_ingest") -> CSRMatrix:
    try:
        matrix = CSRMatrix.from_json_dict(data)
    except LedgerError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError(
            "E_PARSE",
            f"CSR input cannot be parsed: {exc}",
            stage=stage,
        ) from exc
    validate_csr(matrix, stage=stage)
    return matrix
