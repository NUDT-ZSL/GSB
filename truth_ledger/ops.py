from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .hashing import arrays_digest
from .model import CSRFragment, CSRMatrix


@dataclass
class SparseOutput:
    nrows: int
    ncols: int
    row_ptr: np.ndarray
    col_idx: np.ndarray
    values: np.ndarray
    contrib_digest: str
    output_digest: str

    def __post_init__(self) -> None:
        self.row_ptr = np.asarray(self.row_ptr, dtype=np.int64)
        self.col_idx = np.asarray(self.col_idx, dtype=np.int64)
        self.values = np.asarray(self.values, dtype=np.float64)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "nrows": self.nrows,
            "ncols": self.ncols,
            "row_ptr": self.row_ptr.tolist(),
            "col_idx": self.col_idx.tolist(),
            "values": self.values.tolist(),
            "contrib_digest": self.contrib_digest,
            "output_digest": self.output_digest,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "SparseOutput":
        return cls(
            nrows=int(data["nrows"]),
            ncols=int(data["ncols"]),
            row_ptr=np.asarray(data["row_ptr"], dtype=np.int64),
            col_idx=np.asarray(data["col_idx"], dtype=np.int64),
            values=np.asarray(data["values"], dtype=np.float64),
            contrib_digest=str(data["contrib_digest"]),
            output_digest=str(data["output_digest"]),
        )


def spmm_arrays(
    row_ptr: np.ndarray,
    col_idx: np.ndarray,
    values: np.ndarray,
    row_count: int,
    dense_b: np.ndarray,
) -> tuple[np.ndarray, str, str]:
    b = np.asarray(dense_b, dtype=np.float64)
    k = int(b.shape[1])
    output = np.zeros((row_count, k), dtype=np.float64)
    contributions: list[np.ndarray] = []

    for row in range(row_count):
        start = int(row_ptr[row])
        end = int(row_ptr[row + 1])
        accumulator = np.zeros(k, dtype=np.float64)
        for entry in range(start, end):
            contribution = float(values[entry]) * b[int(col_idx[entry]), :]
            accumulator += contribution
            contributions.append(np.asarray(contribution, dtype=np.float64))
        output[row, :] = accumulator

    contribution_array = (
        np.stack(contributions, axis=0).reshape(-1)
        if contributions
        else np.asarray([], dtype=np.float64)
    )
    contrib_digest = arrays_digest(
        f"SPMM-CONTRIB/v1/k/{k}", values=contribution_array
    )
    output_digest = arrays_digest(f"SPMM-OUTPUT/v1/k/{k}", values=output)
    return output, contrib_digest, output_digest


def spmm_fragment(
    fragment: CSRFragment, dense_b: np.ndarray
) -> tuple[np.ndarray, str, str]:
    return spmm_arrays(
        fragment.row_ptr,
        fragment.col_idx,
        fragment.values,
        fragment.nrows,
        dense_b,
    )


def spmm_full(matrix: CSRMatrix, dense_b: np.ndarray) -> tuple[np.ndarray, str, str]:
    return spmm_arrays(
        matrix.row_ptr,
        matrix.col_idx,
        matrix.values,
        matrix.nrows,
        dense_b,
    )


def spmspm_arrays(
    row_ptr: np.ndarray,
    col_idx: np.ndarray,
    values: np.ndarray,
    row_count: int,
    sparse_b: CSRMatrix,
) -> SparseOutput:
    q = int(sparse_b.ncols)
    out_ptr = [0]
    out_cols: list[int] = []
    out_values: list[float] = []
    contribution_cols: list[int] = []
    contribution_values: list[float] = []

    for row in range(row_count):
        accumulator = np.zeros(q, dtype=np.float64)
        start = int(row_ptr[row])
        end = int(row_ptr[row + 1])
        for entry in range(start, end):
            upstream_row = int(col_idx[entry])
            a_value = float(values[entry])
            upstream_start = int(sparse_b.row_ptr[upstream_row])
            upstream_end = int(sparse_b.row_ptr[upstream_row + 1])
            for upstream_entry in range(upstream_start, upstream_end):
                output_col = int(sparse_b.col_idx[upstream_entry])
                contribution = a_value * float(sparse_b.values[upstream_entry])
                accumulator[output_col] += contribution
                contribution_cols.append(output_col)
                contribution_values.append(contribution)

        nonzero_cols = np.flatnonzero(accumulator != 0.0)
        for output_col in nonzero_cols:
            out_cols.append(int(output_col))
            out_values.append(float(accumulator[int(output_col)]))
        out_ptr.append(len(out_cols))

    contrib_digest = arrays_digest(
        "SPMSPM-CONTRIB/v1",
        col_idx=np.asarray(contribution_cols, dtype=np.int64),
        values=np.asarray(contribution_values, dtype=np.float64),
    )
    row_ptr_array = np.asarray(out_ptr, dtype=np.int64)
    col_array = np.asarray(out_cols, dtype=np.int64)
    value_array = np.asarray(out_values, dtype=np.float64)
    output_digest = arrays_digest(
        f"SPMSPM-OUTPUT/v1/q/{q}",
        row_ptr=row_ptr_array,
        col_idx=col_array,
        values=value_array,
    )
    return SparseOutput(
        nrows=row_count,
        ncols=q,
        row_ptr=row_ptr_array,
        col_idx=col_array,
        values=value_array,
        contrib_digest=contrib_digest,
        output_digest=output_digest,
    )


def spmspm_fragment(fragment: CSRFragment, sparse_b: CSRMatrix) -> SparseOutput:
    return spmspm_arrays(
        fragment.row_ptr,
        fragment.col_idx,
        fragment.values,
        fragment.nrows,
        sparse_b,
    )


def spmspm_full(matrix: CSRMatrix, sparse_b: CSRMatrix) -> SparseOutput:
    return spmspm_arrays(
        matrix.row_ptr,
        matrix.col_idx,
        matrix.values,
        matrix.nrows,
        sparse_b,
    )
