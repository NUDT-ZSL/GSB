"""Deterministic CSR and dense-operand reconstruction from a case seed."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import sparse

from .codes import E_CSR_INVALID, E_SHAPE_INVALID
from .errors import SparseOperatorError
from .fingerprint import bytes_fingerprint, csr_fingerprint, dense_fingerprint
from .models import CaseSpec, Operands
from .validation import split_total


def _generation_defaults(spec: CaseSpec) -> dict[str, Any]:
    source = spec.generation or {}
    return {
        "version": source.get("version", 1),
        "a_stream": int(source.get("a_stream", 1)),
        "b_stream": int(source.get("b_stream", 2)),
        "d_stream": int(source.get("d_stream", 3)),
        "a_value_scale": float(source.get("a_value_scale", 1.0)),
        "b_value_scale": float(source.get("b_value_scale", 1.0)),
        "subnormal_fraction": float(source.get("subnormal_fraction", 0.0)),
        "subnormal_scale": float(source.get("subnormal_scale", 5e-324)),
    }


def _rng(seed: int, stream: int, batch_index: int) -> np.random.Generator:
    seed_sequence = np.random.SeedSequence([seed, stream, batch_index])
    return np.random.Generator(np.random.PCG64(seed_sequence))


def _generate_csr(
    rows: int,
    cols: int,
    nnz: int,
    *,
    seed: int,
    stream: int,
    batch_index: int,
    dtype: str,
    value_scale: float,
    subnormal_fraction: float,
    subnormal_scale: float,
) -> sparse.csr_matrix:
    capacity = rows * cols
    if nnz < 0 or nnz > capacity:
        raise SparseOperatorError(
            E_SHAPE_INVALID,
            "requested nnz exceeds matrix capacity",
            phase="reconstruction",
            field="nnz",
            details={"rows": rows, "cols": cols, "nnz": nnz},
        )
    rng = _rng(seed, stream, batch_index)
    if nnz:
        flat = np.sort(rng.choice(capacity, size=nnz, replace=False).astype(np.int64))
    else:
        flat = np.empty(0, dtype=np.int64)
    row_indices = flat // cols
    col_indices = (flat % cols).astype(np.int64)
    row_counts = np.bincount(row_indices, minlength=rows)
    indptr = np.empty(rows + 1, dtype=np.int64)
    indptr[0] = 0
    np.cumsum(row_counts, out=indptr[1:])
    data = rng.standard_normal(nnz) * value_scale
    subnormal_count = min(nnz, int(nnz * subnormal_fraction))
    if subnormal_count:
        data[:subnormal_count] = rng.standard_normal(subnormal_count) * subnormal_scale
    return sparse.csr_matrix(
        (data.astype(dtype), col_indices, indptr),
        shape=(rows, cols),
        dtype=np.dtype(dtype),
    )


def _generate_dense(
    rows: int,
    cols: int,
    *,
    seed: int,
    stream: int,
    batch_index: int,
    dtype: str,
    value_scale: float,
) -> np.ndarray:
    rng = _rng(seed, stream, batch_index)
    array = rng.standard_normal((rows, cols)) * value_scale
    return np.asarray(array, dtype=dtype)


def build_operands(spec: CaseSpec) -> Operands:
    settings = _generation_defaults(spec)
    batch = spec.batch_size
    a_counts = split_total(spec.nnz_a, batch)
    a_items = []
    for index, count in enumerate(a_counts):
        a_items.append(
            _generate_csr(
                spec.m,
                spec.k,
                count,
                seed=spec.seed,
                stream=settings["a_stream"],
                batch_index=index,
                dtype=spec.dtype,
                value_scale=settings["a_value_scale"],
                subnormal_fraction=settings["subnormal_fraction"],
                subnormal_scale=settings["subnormal_scale"],
            )
        )
    if spec.kind == "spmm":
        dense_b = tuple(
            _generate_dense(
                spec.k,
                spec.n,
                seed=spec.seed,
                stream=settings["d_stream"],
                batch_index=index,
                dtype=spec.dtype,
                value_scale=settings["b_value_scale"],
            )
            for index in range(batch)
        )
        return Operands(a=tuple(a_items), b_dense=dense_b)

    b_counts = split_total(spec.nnz_b, batch)
    b_items = []
    for index, count in enumerate(b_counts):
        b_items.append(
            _generate_csr(
                spec.k,
                spec.n,
                count,
                seed=spec.seed,
                stream=settings["b_stream"],
                batch_index=index,
                dtype=spec.dtype,
                value_scale=settings["b_value_scale"],
                subnormal_fraction=settings["subnormal_fraction"],
                subnormal_scale=settings["subnormal_scale"],
            )
        )
    return Operands(a=tuple(a_items), b_sparse=tuple(b_items))


def densify_reference(spec: CaseSpec, operands: Operands) -> np.ndarray:
    output = np.empty((spec.batch_size, spec.m, spec.n), dtype=np.float64)
    for index in range(spec.batch_size):
        a_dense = np.asarray(operands.a[index].toarray(), dtype=np.float64)
        if spec.kind == "spmm":
            assert operands.b_dense is not None
            b_dense = np.asarray(operands.b_dense[index], dtype=np.float64)
        else:
            assert operands.b_sparse is not None
            b_dense = np.asarray(operands.b_sparse[index].toarray(), dtype=np.float64)
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            output[index] = a_dense @ b_dense
    if not np.all(np.isfinite(output)):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "the dense float64 reference produced NaN or infinity",
            case_id=spec.case_id,
            phase="reconstruction",
            field="reference",
        )
    return output


def operand_checksums(spec: CaseSpec, operands: Operands) -> dict[str, Any]:
    checksums: dict[str, Any] = {
        "a": [csr_fingerprint(item) for item in operands.a],
    }
    if spec.kind == "spmm":
        assert operands.b_dense is not None
        checksums["b"] = [dense_fingerprint(item) for item in operands.b_dense]
    else:
        assert operands.b_sparse is not None
        checksums["b"] = [csr_fingerprint(item) for item in operands.b_sparse]
    return checksums


def reference_checksum(reference: np.ndarray) -> str:
    return bytes_fingerprint(reference)

