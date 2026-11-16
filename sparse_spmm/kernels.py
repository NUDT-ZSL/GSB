"""Reference and sparse-row-blocked SpMM kernels with deterministic metadata."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .errors import BlockParameterError, StrategyError
from .utils import float32_row_digest, float64_row_digest, matrix_digest, stable_text_hash


VALID_ORDERS = ("asc", "desc", "b_abs_desc")


@dataclass
class RowTrace:
    """Raw FP32 rounding ledger for one row.

    ``contributions`` has shape ``[row_nnz, N, 3]`` and stores product,
    chunk-local addition and allocated chunk-merge rounding in accumulator
    units. ``positions`` has shape ``[row_nnz, N, 2]`` with chunk index and
    chunk-local offset. ``terms`` stores exact float64 products and
    ``term_magnitudes`` stores absolute rounded products.
    """

    contributions: np.ndarray
    positions: np.ndarray
    chunk_ranges: list[tuple[int, int]]
    terms: np.ndarray
    term_magnitudes: np.ndarray
    epilogue_rounding: np.ndarray
    merge_allocation: str = "proportional_abs_term"


@dataclass
class RowKernelResult:
    row: int
    values: np.ndarray
    record: dict[str, Any]
    trace: RowTrace | None = None


def validate_block_size(block_size: int, *, K: int, stage: str = "block-validation") -> int:
    if isinstance(block_size, bool) or not isinstance(block_size, (int, np.integer)):
        raise BlockParameterError(
            "block size must be an integer",
            stage=stage,
            shape={"K": int(K)},
            details={"block_size": repr(block_size)},
        )
    value = int(block_size)
    if value <= 0 or value > int(K):
        raise BlockParameterError(
            "block size must satisfy 1 <= block_size <= K",
            stage=stage,
            shape={"K": int(K)},
            details={"block_size": value},
        )
    return value


def validate_order(order: str, *, stage: str = "order-validation") -> str:
    if order not in VALID_ORDERS:
        raise StrategyError(
            "accumulation order must be asc, desc or b_abs_desc",
            stage=stage,
            details={"order": repr(order), "allowed": list(VALID_ORDERS)},
        )
    return order


def _as_dtype(dtype: Any) -> np.dtype[Any]:
    result = np.dtype(dtype)
    if result not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise ValueError("kernel dtype must be float32 or float64")
    return result


def _row_digest(values: np.ndarray, dtype: np.dtype[Any]) -> str:
    return float32_row_digest(values) if dtype == np.dtype(np.float32) else float64_row_digest(values)


def _chunk_ranges(indptr: np.ndarray, row: int, block_size: int) -> list[tuple[int, int]]:
    start = int(indptr[row])
    end = int(indptr[row + 1])
    return [(offset, min(offset + block_size, end)) for offset in range(start, end, block_size)]


def _reference_order_digest(nonzero_ids: list[str], dtype: np.dtype[Any]) -> str:
    return stable_text_hash([f"REF_COL_ASC|{dtype.name}", *nonzero_ids])


def reference_row(
    csr: CSRMatrix,
    B: np.ndarray,
    C: np.ndarray,
    *,
    row: int,
    alpha: float,
    beta: float,
    dtype: Any = np.float32,
) -> RowKernelResult:
    """Column-ascending axpy reference path for one row and one precision tier."""

    work_dtype = _as_dtype(dtype)
    B_work = np.asarray(B, dtype=work_dtype)
    C_work = np.asarray(C, dtype=work_dtype)
    A_work = np.asarray(csr.data, dtype=work_dtype)
    N = int(B_work.shape[1])
    start = int(csr.indptr[row])
    end = int(csr.indptr[row + 1])
    accumulator = np.zeros(N, dtype=work_dtype)
    for position in range(start, end):
        col = int(csr.indices[position])
        term = np.multiply(A_work[position], B_work[col], dtype=work_dtype)
        accumulator = np.add(accumulator, term, dtype=work_dtype)
    alpha_d = np.array(alpha, dtype=work_dtype)[()]
    beta_d = np.array(beta, dtype=work_dtype)[()]
    scaled = np.multiply(alpha_d, accumulator, dtype=work_dtype)
    beta_term = np.multiply(beta_d, C_work[row], dtype=work_dtype)
    values = np.add(scaled, beta_term, dtype=work_dtype)
    ids = csr.nonzero_ids[start:end]
    record = {
        "row": int(row),
        "nnz": int(end - start),
        "order_id": "REF_COL_ASC",
        "order_digest": _reference_order_digest(ids, work_dtype),
        "first_nonzero_ids": ids[:3],
        "last_nonzero_ids": ids[-3:],
        "result_fingerprint": _row_digest(values, work_dtype),
    }
    return RowKernelResult(row=int(row), values=values, record=record)


def _ordered_positions(
    positions: np.ndarray,
    sparse_columns: np.ndarray,
    B: np.ndarray,
    *,
    order: str,
) -> np.ndarray:
    """Return a [chunk_length, N] matrix of global CSR positions per output column."""

    N = int(B.shape[1])
    if order == "asc":
        return np.repeat(positions[:, None], N, axis=1)
    if order == "desc":
        return np.repeat(positions[::-1, None], N, axis=1)
    ordered = np.empty((positions.size, N), dtype=np.int64)
    for col in range(N):
        magnitudes = np.abs(B[sparse_columns, col].astype(np.float64))
        local_order = np.argsort(-magnitudes, kind="stable")
        ordered[:, col] = positions[local_order]
    return ordered


def _order_hashers(row: int, order: str, block_size: int, dtype: np.dtype[Any], N: int) -> list[Any]:
    hashers = []
    prefix = f"row={row}|dtype={dtype.name}|order={order}|bs={block_size}|".encode("utf-8")
    for col in range(N):
        hasher = hashlib.sha256()
        hasher.update(prefix)
        hasher.update(f"col={col}|".encode("utf-8"))
        hashers.append(hasher)
    return hashers


def _finish_order_hashes(hashers: list[Any]) -> tuple[list[str], str]:
    column_digests = [hasher.hexdigest() for hasher in hashers]
    combined = stable_text_hash(column_digests)
    return column_digests, combined


def chunked_row(
    csr: CSRMatrix,
    B: np.ndarray,
    C: np.ndarray,
    *,
    row: int,
    alpha: float,
    beta: float,
    block_size: int,
    order: str,
    dtype: Any = np.float32,
    trace: bool = False,
) -> RowKernelResult:
    """Two-stage sparse row blocking with configurable within-chunk order.

    The second-stage merge always traverses chunk indices in ascending order.
    Only within a chunk does ``order`` change the operation sequence. This keeps
    thread scheduling from ever affecting numerical order.
    """

    work_dtype = _as_dtype(dtype)
    validate_block_size(block_size, K=csr.cols)
    validate_order(order)
    B_work = np.asarray(B, dtype=work_dtype)
    C_work = np.asarray(C, dtype=work_dtype)
    A_work = np.asarray(csr.data, dtype=work_dtype)
    N = int(B_work.shape[1])
    row_start = int(csr.indptr[row])
    row_end = int(csr.indptr[row + 1])
    row_nnz = row_end - row_start
    ranges = _chunk_ranges(csr.indptr, row, block_size)
    hashers = _order_hashers(row, order, block_size, work_dtype, N)
    column_index = np.arange(N, dtype=np.int64)

    if trace:
        contributions = np.zeros((row_nnz, N, 3), dtype=np.float64)
        positions_trace = np.full((row_nnz, N, 2), -1, dtype=np.int32)
        exact_terms = np.zeros((row_nnz, N), dtype=np.float64)
        term_magnitudes = np.zeros((row_nnz, N), dtype=np.float64)
    else:
        contributions = positions_trace = exact_terms = term_magnitudes = None

    partials: list[np.ndarray] = []
    for chunk_index, (start, end) in enumerate(ranges):
        positions = np.arange(start, end, dtype=np.int64)
        sparse_columns = csr.indices[positions].astype(np.int64, copy=False)
        ordered = _ordered_positions(positions, sparse_columns, B_work, order=order)
        partial = np.zeros(N, dtype=work_dtype)
        for offset in range(ordered.shape[0]):
            logical_positions = ordered[offset]
            nonzero_ids = [
                csr.nonzero_ids[int(logical_positions[col])] for col in range(N)
            ]
            for col, nz_id in enumerate(nonzero_ids):
                hashers[col].update(nz_id.encode("utf-8"))
                hashers[col].update(b"|")
            coefficients = A_work[logical_positions]
            dense_rows = csr.indices[logical_positions].astype(np.int64, copy=False)
            dense_values = B_work[dense_rows, column_index]
            terms = np.multiply(coefficients, dense_values, dtype=work_dtype)
            previous = partial.copy() if trace else None
            partial = np.add(partial, terms, dtype=work_dtype)
            if trace:
                local_rows = logical_positions - row_start
                coeff64 = coefficients.astype(np.float64)
                dense64 = dense_values.astype(np.float64)
                term64 = terms.astype(np.float64)
                exact = coeff64 * dense64
                product_rounding = term64 - exact
                add_rounding = partial.astype(np.float64) - (
                    previous.astype(np.float64) + term64
                )
                contributions[local_rows, column_index, 0] += product_rounding
                contributions[local_rows, column_index, 1] += add_rounding
                positions_trace[local_rows, column_index, 0] = chunk_index
                positions_trace[local_rows, column_index, 1] = offset
                exact_terms[local_rows, column_index] = exact
                term_magnitudes[local_rows, column_index] = np.abs(term64)
        partials.append(partial)
        for col in range(N):
            hashers[col].update(f"#merge:{chunk_index}|".encode("utf-8"))

    accumulator = np.zeros(N, dtype=work_dtype)
    epilogue = np.zeros((3, N), dtype=np.float64) if trace else None
    for chunk_index, partial in enumerate(partials):
        previous = accumulator.copy()
        accumulator = np.add(accumulator, partial, dtype=work_dtype)
        if trace:
            start, end = ranges[chunk_index]
            local_slice = slice(start - row_start, end - row_start)
            merge_rounding = accumulator.astype(np.float64) - (
                previous.astype(np.float64) + partial.astype(np.float64)
            )
            weights = term_magnitudes[local_slice, :]
            weight_sum = weights.sum(axis=0)
            for col in range(N):
                if weight_sum[col] > 0.0:
                    allocated = merge_rounding[col] * weights[:, col] / weight_sum[col]
                else:
                    allocated = np.zeros(end - start, dtype=np.float64)
                    allocated[-1] = merge_rounding[col]
                contributions[local_slice, col, 2] += allocated

    alpha_d = np.array(alpha, dtype=work_dtype)[()]
    beta_d = np.array(beta, dtype=work_dtype)[()]
    scaled = np.multiply(alpha_d, accumulator, dtype=work_dtype)
    beta_term = np.multiply(beta_d, C_work[row], dtype=work_dtype)
    values = np.add(scaled, beta_term, dtype=work_dtype)
    if trace:
        alpha64 = float(alpha)
        beta64 = float(beta)
        accumulator64 = accumulator.astype(np.float64)
        scaled64 = scaled.astype(np.float64)
        beta_term64 = beta_term.astype(np.float64)
        C64 = C_work[row].astype(np.float64)
        epilogue[0, :] = scaled64 - alpha64 * accumulator64
        epilogue[1, :] = beta_term64 - beta64 * C64
        epilogue[2, :] = values.astype(np.float64) - (scaled64 + beta_term64)

    column_digests, order_digest = _finish_order_hashes(hashers)
    ids = csr.nonzero_ids[row_start:row_end]
    record = {
        "row": int(row),
        "nnz": int(row_nnz),
        "order_id": f"CHUNKED_{order.upper()}|bs={block_size}",
        "order_digest": order_digest,
        "column_order_digests": column_digests,
        "chunk_count": len(ranges),
        "chunk_ranges": [[int(start), int(end)] for start, end in ranges],
        "first_nonzero_ids": ids[:3],
        "last_nonzero_ids": ids[-3:],
        "result_fingerprint": _row_digest(values, work_dtype),
    }
    row_trace = None
    if trace:
        row_trace = RowTrace(
            contributions=contributions,
            positions=positions_trace,
            chunk_ranges=ranges,
            terms=exact_terms,
            term_magnitudes=term_magnitudes,
            epilogue_rounding=epilogue,
        )
    return RowKernelResult(row=int(row), values=values, record=record, trace=row_trace)


def kernel_matrix_digest(matrix: np.ndarray, dtype: Any) -> str:
    return matrix_digest(matrix, dtype)
