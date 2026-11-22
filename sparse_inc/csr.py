"""Strict CSR parsing and deterministic dense conversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import sparse

from .canonical import encode_array_f64, sha256_json
from .codes import E_CSR_STRUCT, E_DTYPE_UNSUPPORTED, E_NNZ_MISMATCH, E_SHAPE_INVALID
from .errors import raise_business


SUPPORTED_DTYPES = {"float64": np.float64}


@dataclass(frozen=True)
class CSRMatrix:
    shape: tuple[int, int]
    indptr: np.ndarray
    indices: np.ndarray
    values: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "indptr", _readonly(self.indptr, np.int64))
        object.__setattr__(self, "indices", _readonly(self.indices, np.int64))
        object.__setattr__(self, "values", _readonly(self.values, np.float64))

    @property
    def nnz(self) -> int:
        return int(self.values.size)

    def to_scipy(self) -> sparse.csr_matrix:
        return sparse.csr_matrix(
            (self.values, self.indices, self.indptr),
            shape=self.shape,
            dtype=np.float64,
        )

    def to_dense(self) -> np.ndarray:
        return np.asarray(self.to_scipy().toarray(), dtype=np.float64)

    def payload(self) -> dict[str, Any]:
        return {
            "shape": list(self.shape),
            "indptr": encode_array_f64(np.asarray(self.indptr, dtype=np.int64)),
            "indices": encode_array_f64(np.asarray(self.indices, dtype=np.int64)),
            "values": encode_array_f64(self.values),
        }

    @property
    def checksum(self) -> str:
        return sha256_json(self.payload())

    def row_entries(self, row: int) -> tuple[tuple[int, ...], tuple[float, ...]]:
        start = int(self.indptr[row])
        end = int(self.indptr[row + 1])
        cols = tuple(int(item) for item in self.indices[start:end])
        vals = tuple(float(item) for item in self.values[start:end])
        return cols, vals


def _readonly(array: np.ndarray, dtype: Any) -> np.ndarray:
    arr = np.ascontiguousarray(array, dtype=dtype).copy()
    arr.setflags(write=False)
    return arr


def validate_dtype(dtype: Any, *, case_id: str, segment: str) -> None:
    if dtype not in SUPPORTED_DTYPES:
        raise_business(
            E_DTYPE_UNSUPPORTED,
            case_id=case_id,
            segment=segment,
            message="only float64 is supported",
            declared_dtype=dtype,
            supported_dtypes=sorted(SUPPORTED_DTYPES),
        )


def parse_csr(
    raw: Any,
    *,
    expected_shape: tuple[int, int],
    declared_nnz: int,
    dtype: str,
    case_id: str,
    segment: str,
) -> CSRMatrix:
    validate_dtype(dtype, case_id=case_id, segment=f"{segment}.dtype")
    rows, cols = expected_shape
    if rows <= 0 or cols <= 0:
        raise_business(
            E_SHAPE_INVALID,
            case_id=case_id,
            segment=segment,
            message="CSR shape dimensions must be positive",
            expected_shape=list(expected_shape),
        )
    if not isinstance(raw, dict):
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="CSR payload must be an object",
        )
    if isinstance(declared_nnz, bool) or not isinstance(declared_nnz, int) or declared_nnz < 0:
        raise_business(
            E_NNZ_MISMATCH,
            case_id=case_id,
            segment=f"{segment}.nnz",
            message="declared nnz must be a non-negative integer",
            declared_nnz=declared_nnz,
        )

    indptr = _int_array(raw.get("indptr"), f"{segment}.indptr", case_id)
    indices = _int_array(raw.get("indices"), f"{segment}.indices", case_id)
    values = _float_array(raw.get("values"), f"{segment}.values", case_id)

    if indptr.size != rows + 1:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="indptr length must equal rows + 1",
            expected_length=rows + 1,
            actual_length=int(indptr.size),
        )
    if indices.size != declared_nnz or values.size != declared_nnz:
        raise_business(
            E_NNZ_MISMATCH,
            case_id=case_id,
            segment=f"{segment}.nnz",
            message="declared nnz does not match stored indices/values",
            declared_nnz=declared_nnz,
            stored_indices=int(indices.size),
            stored_values=int(values.size),
        )
    if int(indptr[0]) != 0:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="indptr[0] must be zero",
            actual=int(indptr[0]),
        )
    if np.any(indptr < 0) or np.any(indptr > declared_nnz):
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="indptr entries must lie in [0, nnz]",
        )
    if np.any(indptr[1:] < indptr[:-1]):
        bad_row = int(np.nonzero(indptr[1:] < indptr[:-1])[0][0])
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="indptr must be monotonic non-decreasing",
            row=bad_row,
            previous=int(indptr[bad_row]),
            current=int(indptr[bad_row + 1]),
        )
    if int(indptr[-1]) != declared_nnz:
        raise_business(
            E_NNZ_MISMATCH,
            case_id=case_id,
            segment=f"{segment}.indptr",
            message="indptr[-1] must equal declared nnz",
            declared_nnz=declared_nnz,
            indptr_last=int(indptr[-1]),
        )
    if indices.size and (np.any(indices < 0) or np.any(indices >= cols)):
        position = int(np.nonzero((indices < 0) | (indices >= cols))[0][0])
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.indices",
            message="column index is outside the declared shape",
            position=position,
            column_index=int(indices[position]),
            column_bound=cols,
        )
    if not np.all(np.isfinite(values)):
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=f"{segment}.values",
            message="CSR values must be finite float64 numbers",
        )
    return CSRMatrix(
        shape=expected_shape,
        indptr=indptr,
        indices=indices,
        values=values,
    )


def _int_array(raw: Any, segment: str, case_id: str) -> np.ndarray:
    if not isinstance(raw, (list, tuple)):
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message="CSR integer array must be a JSON array",
        )
    for position, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (int, np.integer)):
            raise_business(
                E_CSR_STRUCT,
                case_id=case_id,
                segment=f"{segment}[{position}]",
                message="CSR integer array contains a non-integer value",
                value=item,
            )
    try:
        arr = np.asarray(raw, dtype=np.int64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message=f"array must contain integers: {exc}",
        )
    if arr.ndim != 1:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message="CSR arrays must be one-dimensional",
            actual_ndim=int(arr.ndim),
        )
    return arr


def _float_array(raw: Any, segment: str, case_id: str) -> np.ndarray:
    if not isinstance(raw, (list, tuple)):
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message="CSR value array must be a JSON array",
        )
    for position, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(
            item,
            (int, float, np.integer, np.floating),
        ):
            raise_business(
                E_CSR_STRUCT,
                case_id=case_id,
                segment=f"{segment}[{position}]",
                message="CSR value array contains a non-numeric value",
                value=item,
            )
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message=f"array must contain float64 values: {exc}",
        )
    if arr.ndim != 1:
        raise_business(
            E_CSR_STRUCT,
            case_id=case_id,
            segment=segment,
            message="CSR arrays must be one-dimensional",
            actual_ndim=int(arr.ndim),
        )
    return arr
