"""Metadata parsing and strict CSR/dense operand validation."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import sparse

from .codes import (
    E_CSR_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_METADATA_INVALID,
    E_SHAPE_INVALID,
)
from .errors import SparseOperatorError
from .fingerprint import csr_fingerprint, dense_fingerprint
from .models import CaseSpec, Operands, SUPPORTED_DTYPES, SUPPORTED_KINDS


def _as_int(value: Any, field: str, case_id: str | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SparseOperatorError(
            E_METADATA_INVALID,
            f"{field} must be an integer",
            case_id=case_id,
            phase="metadata",
            field=field,
        )
    return value


def _as_shape_int(value: Any, field: str, case_id: str | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            f"{field} must contain integer dimensions",
            case_id=case_id,
            phase="validation",
            field="shape_mnk",
        )
    return value


def parse_case_spec(metadata: dict[str, Any]) -> CaseSpec:
    if not isinstance(metadata, dict):
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "metadata must be a JSON object",
            phase="metadata",
            field="metadata",
        )
    case_id_value = metadata.get("case_id")
    case_id = case_id_value if isinstance(case_id_value, str) and case_id_value else None
    if case_id is None:
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "case_id must be a non-empty string",
            phase="metadata",
            field="case_id",
        )

    shape = metadata.get("shape_mnk")
    if not isinstance(shape, (list, tuple)) or len(shape) not in {3, 4}:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "shape_mnk must be [m, k, n] or [batch, m, k, n]",
            case_id=case_id,
            phase="validation",
            field="shape_mnk",
        )
    parsed_shape = [
        _as_shape_int(item, "shape_mnk", case_id) for item in shape
    ]
    if any(item <= 0 for item in parsed_shape):
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "all dimensions in shape_mnk must be positive",
            case_id=case_id,
            phase="validation",
            field="shape_mnk",
            details={"shape_mnk": parsed_shape},
        )
    if len(parsed_shape) == 4:
        batch_shape = parsed_shape[0]
        m, k, n = parsed_shape[1:]
        meta_batch = metadata.get("batch_size", batch_shape)
        batch_size = _as_shape_int(meta_batch, "batch_size", case_id)
        if batch_size != batch_shape:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "batch_size does not match the leading shape_mnk dimension",
                case_id=case_id,
                phase="validation",
                field="shape_mnk",
                details={"batch_size": batch_size, "shape_mnk": parsed_shape},
            )
    else:
        m, k, n = parsed_shape
        batch_size = _as_shape_int(
            metadata.get("batch_size", 1), "batch_size", case_id
        )
    if batch_size <= 0:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "batch_size must be positive",
            case_id=case_id,
            phase="validation",
            field="batch_size",
        )

    dtype_value = metadata.get("dtype")
    if not isinstance(dtype_value, str):
        raise SparseOperatorError(
            E_DTYPE_UNSUPPORTED,
            "dtype must be a string",
            case_id=case_id,
            phase="validation",
            field="dtype",
        )
    dtype = dtype_value.lower()
    if dtype not in SUPPORTED_DTYPES:
        raise SparseOperatorError(
            E_DTYPE_UNSUPPORTED,
            f"unsupported dtype: {dtype_value}",
            case_id=case_id,
            phase="validation",
            field="dtype",
            details={"supported_dtypes": sorted(SUPPORTED_DTYPES)},
        )

    kind = metadata.get("kind")
    if kind is None:
        b_storage_hint = metadata.get("b_storage")
        kind = "spmm" if b_storage_hint == "dense" else "spspmm"
    if kind not in SUPPORTED_KINDS:
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "kind must be 'spmm' or 'spspmm'",
            case_id=case_id,
            phase="metadata",
            field="kind",
        )

    b_storage = metadata.get(
        "b_storage", "dense" if kind == "spmm" else "sparse"
    )
    expected_storage = "dense" if kind == "spmm" else "sparse"
    if b_storage != expected_storage:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            f"{kind} requires b_storage={expected_storage}",
            case_id=case_id,
            phase="validation",
            field="b_storage",
            details={"b_storage": b_storage},
        )

    nnz_a = _as_int(metadata.get("nnz_a"), "nnz_a", case_id)
    nnz_b = _as_int(metadata.get("nnz_b"), "nnz_b", case_id)
    if nnz_a < 0 or nnz_a > batch_size * m * k:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "nnz_a is outside the valid range",
            case_id=case_id,
            phase="validation",
            field="nnz_a",
            details={"nnz_a": nnz_a, "capacity": batch_size * m * k},
        )
    max_nnz_b = batch_size * (k * n if kind == "spmm" else k * n)
    if nnz_b < 0 or nnz_b > max_nnz_b:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "nnz_b is outside the valid range",
            case_id=case_id,
            phase="validation",
            field="nnz_b",
            details={"nnz_b": nnz_b, "capacity": max_nnz_b},
        )
    if kind == "spmm" and nnz_b != batch_size * k * n:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "for SpMM, nnz_b must equal the number of dense entries batch*k*n",
            case_id=case_id,
            phase="validation",
            field="nnz_b",
            details={"nnz_b": nnz_b, "expected": batch_size * k * n},
        )

    seed = _as_int(metadata.get("seed"), "seed", case_id)
    if seed < 0:
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "seed must be non-negative",
            case_id=case_id,
            phase="metadata",
            field="seed",
        )

    raw_candidates = metadata.get("block_candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "block_candidates must be a non-empty list",
            case_id=case_id,
            phase="metadata",
            field="block_candidates",
        )
    candidates: list[int] = []
    for position, item in enumerate(raw_candidates):
        value = _as_int(item, f"block_candidates[{position}]", case_id)
        if value <= 0:
            raise SparseOperatorError(
                E_METADATA_INVALID,
                "block candidates must be positive",
                case_id=case_id,
                phase="metadata",
                field=f"block_candidates[{position}]",
            )
        candidates.append(value)

    generation = metadata.get("generation") or {}
    checksums = metadata.get("checksums") or {}
    if not isinstance(generation, dict):
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "generation must be an object",
            case_id=case_id,
            phase="metadata",
            field="generation",
        )
    if not isinstance(checksums, dict):
        raise SparseOperatorError(
            E_METADATA_INVALID,
            "checksums must be an object",
            case_id=case_id,
            phase="metadata",
            field="checksums",
        )

    return CaseSpec(
        case_id=case_id,
        kind=kind,
        shape_mnk=(m, k, n),
        batch_size=batch_size,
        nnz_a=nnz_a,
        nnz_b=nnz_b,
        dtype=dtype,
        seed=seed,
        block_candidates=tuple(candidates),
        generation=generation,
        checksums=checksums,
        b_storage=b_storage,
        raw=metadata,
    )


def _dtype_name(dtype: np.dtype[Any]) -> str:
    return np.dtype(dtype).name


def _check_numeric_dtype(dtype: np.dtype[Any], case_id: str, field: str) -> None:
    name = _dtype_name(dtype)
    if name not in SUPPORTED_DTYPES:
        raise SparseOperatorError(
            E_DTYPE_UNSUPPORTED,
            f"unsupported operand dtype: {name}",
            case_id=case_id,
            phase="validation",
            field=field,
            details={"supported_dtypes": sorted(SUPPORTED_DTYPES)},
        )


def _check_csr(matrix: Any, case_id: str, field: str) -> None:
    if not sparse.issparse(matrix):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "operand is not a SciPy sparse matrix",
            case_id=case_id,
            phase="validation",
            field=field,
        )
    if matrix.format != "csr":
        matrix = matrix.tocsr()
    _check_numeric_dtype(matrix.dtype, case_id, f"{field}.dtype")
    indptr = np.asarray(matrix.indptr)
    indices = np.asarray(matrix.indices)
    data = np.asarray(matrix.data)
    rows, cols = matrix.shape
    if indptr.ndim != 1 or indptr.size != rows + 1:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indptr must have length nrows+1",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indptr",
            details={"length": int(indptr.size), "expected": rows + 1},
        )
    if not np.issubdtype(indptr.dtype, np.integer):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indptr must use an integer dtype",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indptr",
        )
    if indptr[0] != 0:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indptr[0] must be zero",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indptr",
            details={"indptr_0": int(indptr[0])},
        )
    if np.any(np.diff(indptr) < 0):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indptr must be monotonic nondecreasing",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indptr",
        )
    if indices.ndim != 1 or indices.size != data.size:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indices and data must be one-dimensional and have equal length",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indices",
            details={"indices_size": int(indices.size), "data_size": int(data.size)},
        )
    if not np.issubdtype(indices.dtype, np.integer):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indices must use an integer dtype",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indices",
        )
    if int(indptr[-1]) != int(data.size):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "indptr[-1] must equal nnz",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indptr",
            details={"indptr_last": int(indptr[-1]), "nnz": int(data.size)},
        )
    if data.size and (indices.min() < 0 or indices.max() >= cols):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "column indices are out of bounds",
            case_id=case_id,
            phase="validation",
            field=f"{field}.indices",
            details={
                "min_index": int(indices.min()),
                "max_index": int(indices.max()),
                "ncols": cols,
            },
        )
    if data.size and not np.all(np.isfinite(np.asarray(data, dtype=np.float64))):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "CSR data contains NaN or infinity",
            case_id=case_id,
            phase="validation",
            field=f"{field}.data",
        )


def normalize_batches(value: Any, batch_size: int, case_id: str, field: str) -> list[Any]:
    if isinstance(value, np.ndarray) and value.ndim == 3:
        if value.shape[0] != batch_size:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "dense batch dimension does not match batch_size",
                case_id=case_id,
                phase="validation",
                field=field,
                details={"actual": int(value.shape[0]), "expected": batch_size},
            )
        return [value[index] for index in range(batch_size)]
    if batch_size == 1 and not (
        isinstance(value, (list, tuple)) and len(value) == 1
    ):
        return [value]
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) != batch_size:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "operand batch count does not match batch_size",
                case_id=case_id,
                phase="validation",
                field=field,
                details={"actual": len(items), "expected": batch_size},
            )
        return items
    if batch_size == 1:
        return [value]
    raise SparseOperatorError(
        E_SHAPE_INVALID,
        "a batched operand must be a sequence or a 3-D dense array",
        case_id=case_id,
        phase="validation",
        field=field,
    )


def normalize_operands(spec: CaseSpec, a: Any, b: Any) -> Operands:
    a_items = normalize_batches(a, spec.batch_size, spec.case_id, "a")
    b_items = normalize_batches(b, spec.batch_size, spec.case_id, "b")
    normalized_a: list[Any] = []
    for index, item in enumerate(a_items):
        if not sparse.issparse(item):
            raise SparseOperatorError(
                E_CSR_INVALID,
                "A must be a SciPy sparse matrix",
                case_id=spec.case_id,
                phase="validation",
                field=f"a[{index}]",
            )
        normalized_a.append(item.tocsr())
    if spec.kind == "spmm":
        normalized_b: list[np.ndarray] = []
        for index, item in enumerate(b_items):
            if sparse.issparse(item):
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "SpMM requires a dense B/D operand",
                    case_id=spec.case_id,
                    phase="validation",
                    field=f"b[{index}]",
                )
            normalized_b.append(np.asarray(item))
        return Operands(a=tuple(normalized_a), b_dense=tuple(normalized_b))
    normalized_sparse_b: list[Any] = []
    for index, item in enumerate(b_items):
        if not sparse.issparse(item):
            raise SparseOperatorError(
                E_CSR_INVALID,
                "SpSpMM requires a SciPy sparse B operand",
                case_id=spec.case_id,
                phase="validation",
                field=f"b[{index}]",
            )
        normalized_sparse_b.append(item.tocsr())
    return Operands(a=tuple(normalized_a), b_sparse=tuple(normalized_sparse_b))


def validate_operands(
    spec: CaseSpec,
    operands: Operands,
    *,
    verify_checksums: bool = True,
) -> dict[str, Any]:
    if len(operands.a) != spec.batch_size:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "A batch count does not match metadata",
            case_id=spec.case_id,
            phase="validation",
            field="a",
            details={"actual": len(operands.a), "expected": spec.batch_size},
        )
    total_a_nnz = 0
    for index, matrix in enumerate(operands.a):
        field = f"a[{index}]"
        _check_csr(matrix, spec.case_id, field)
        if matrix.shape != (spec.m, spec.k):
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "A shape does not match shape_mnk",
                case_id=spec.case_id,
                phase="validation",
                field=f"{field}.shape",
                details={
                    "actual": list(matrix.shape),
                    "expected": [spec.m, spec.k],
                },
            )
        total_a_nnz += int(matrix.nnz)
    if total_a_nnz != spec.nnz_a:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "declared nnz_a does not match the actual CSR data",
            case_id=spec.case_id,
            phase="validation",
            field="nnz_a",
            details={"declared": spec.nnz_a, "actual": total_a_nnz},
        )

    total_b_nnz = 0
    if spec.kind == "spmm":
        assert operands.b_dense is not None
        if len(operands.b_dense) != spec.batch_size:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "B batch count does not match metadata",
                case_id=spec.case_id,
                phase="validation",
                field="b",
            )
        for index, array in enumerate(operands.b_dense):
            field = f"b[{index}]"
            if array.ndim != 2:
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "dense B must be two-dimensional",
                    case_id=spec.case_id,
                    phase="validation",
                    field=f"{field}.shape",
                )
            _check_numeric_dtype(array.dtype, spec.case_id, f"{field}.dtype")
            if array.shape != (spec.k, spec.n):
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "dense B shape does not match shape_mnk",
                    case_id=spec.case_id,
                    phase="validation",
                    field=f"{field}.shape",
                    details={
                        "actual": list(array.shape),
                        "expected": [spec.k, spec.n],
                    },
                )
            if not np.all(np.isfinite(np.asarray(array, dtype=np.float64))):
                raise SparseOperatorError(
                    E_CSR_INVALID,
                    "dense B contains NaN or infinity",
                    case_id=spec.case_id,
                    phase="validation",
                    field=f"{field}.data",
                )
            total_b_nnz += int(array.size)
    else:
        assert operands.b_sparse is not None
        if len(operands.b_sparse) != spec.batch_size:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "B batch count does not match metadata",
                case_id=spec.case_id,
                phase="validation",
                field="b",
            )
        for index, matrix in enumerate(operands.b_sparse):
            field = f"b[{index}]"
            _check_csr(matrix, spec.case_id, field)
            if matrix.shape != (spec.k, spec.n):
                raise SparseOperatorError(
                    E_SHAPE_INVALID,
                    "B shape does not match shape_mnk",
                    case_id=spec.case_id,
                    phase="validation",
                    field=f"{field}.shape",
                    details={
                        "actual": list(matrix.shape),
                        "expected": [spec.k, spec.n],
                    },
                )
            total_b_nnz += int(matrix.nnz)

    if total_b_nnz != spec.nnz_b:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "declared nnz_b does not match the actual operand",
            case_id=spec.case_id,
            phase="validation",
            field="nnz_b",
            details={"declared": spec.nnz_b, "actual": total_b_nnz},
        )

    if verify_checksums and spec.checksums:
        _verify_checksums(spec, operands)

    return {
        "batch_size": spec.batch_size,
        "a_nnz": total_a_nnz,
        "b_nnz_or_entries": total_b_nnz,
        "b_storage": spec.b_storage,
    }


def _verify_checksums(spec: CaseSpec, operands: Operands) -> None:
    expected_a = spec.checksums.get("a")
    actual_a = [csr_fingerprint(item) for item in operands.a]
    if expected_a is not None and list(expected_a) != actual_a:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "reconstructed A does not match the metadata checksum",
            case_id=spec.case_id,
            phase="reconstruction",
            field="checksums.a",
            details={"expected": list(expected_a), "actual": actual_a},
        )
    if spec.kind == "spmm":
        assert operands.b_dense is not None
        expected_b = spec.checksums.get("b")
        actual_b = [dense_fingerprint(item) for item in operands.b_dense]
    else:
        assert operands.b_sparse is not None
        expected_b = spec.checksums.get("b")
        actual_b = [csr_fingerprint(item) for item in operands.b_sparse]
    if expected_b is not None and list(expected_b) != actual_b:
        raise SparseOperatorError(
            E_CSR_INVALID,
            "reconstructed B does not match the metadata checksum",
            case_id=spec.case_id,
            phase="reconstruction",
            field="checksums.b",
            details={"expected": list(expected_b), "actual": actual_b},
        )


def validate_reference(
    spec: CaseSpec, reference: np.ndarray
) -> np.ndarray:
    array = np.asarray(reference, dtype=np.float64)
    if array.shape != (spec.batch_size, spec.m, spec.n):
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "reference shape does not match batch*m*n",
            case_id=spec.case_id,
            phase="validation",
            field="reference.shape",
            details={
                "actual": list(array.shape),
                "expected": [spec.batch_size, spec.m, spec.n],
            },
        )
    if not np.all(np.isfinite(array)):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "reference contains NaN or infinity",
            case_id=spec.case_id,
            phase="validation",
            field="reference",
        )
    return array


def split_total(total: int, parts: int) -> list[int]:
    quotient, remainder = divmod(total, parts)
    return [quotient + (1 if index < remainder else 0) for index in range(parts)]

