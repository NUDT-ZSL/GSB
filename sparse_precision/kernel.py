"""Blocked sparse execution, Kahan accumulation and intermediate snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import sparse

from .codes import E_RESOURCE_LIMIT
from .errors import SparseOperatorError
from .models import CaseSpec, Operands, OperatorConfig


@dataclass
class ExecutionOutcome:
    result: np.ndarray
    partials: list[list[list[np.ndarray]]]
    block_outputs: list[list[np.ndarray]]
    nnz_processed: int
    nnz_b_processed: int
    aggregation_entries: int
    actual_partial_bytes: int
    coverage_ok: bool
    local_max_abs_error: float
    local_max_position: dict[str, Any] | None
    cross_reduction_max_abs_error: float
    cross_reduction_max_position: dict[str, Any] | None
    row_block_envelope_max_abs_error: float
    row_block_envelope_position: dict[str, Any] | None
    reduction_recompute_max_abs_error: float
    stage_methods: dict[str, str]


def _kahan_add(
    total: np.ndarray, compensation: np.ndarray, value: np.ndarray
) -> None:
    corrected = value - compensation
    updated = total + corrected
    compensation[...] = (updated - total) - corrected
    total[...] = updated


def _local_direct(
    a_csr: sparse.csr_matrix,
    rhs_dense: np.ndarray,
    *,
    compensated: bool,
    reverse: bool,
) -> np.ndarray:
    rows = a_csr.shape[0]
    cols = rhs_dense.shape[1]
    total = np.zeros((rows, cols), dtype=np.float64)
    compensation = np.zeros_like(total) if compensated else None
    indptr = np.asarray(a_csr.indptr)
    indices = np.asarray(a_csr.indices)
    values = np.asarray(a_csr.data, dtype=np.float64)
    rhs = np.asarray(rhs_dense, dtype=np.float64)
    for row in range(rows):
        start = int(indptr[row])
        stop = int(indptr[row + 1])
        positions = range(stop - 1, start - 1, -1) if reverse else range(start, stop)
        for position in positions:
            contribution = values[position] * rhs[int(indices[position])]
            if compensation is None:
                total[row] += contribution
            else:
                _kahan_add(total[row], compensation[row], contribution)
    return total


def _local_aggregated(
    a_csr: sparse.csr_matrix,
    b_csr: sparse.csr_matrix,
    *,
    compensated: bool,
) -> tuple[np.ndarray, int]:
    rows = a_csr.shape[0]
    cols = b_csr.shape[1]
    out_rows: list[int] = []
    out_cols: list[int] = []
    out_values: list[float] = []
    a_indptr = np.asarray(a_csr.indptr)
    a_indices = np.asarray(a_csr.indices)
    a_values = np.asarray(a_csr.data, dtype=np.float64)
    b_indptr = np.asarray(b_csr.indptr)
    b_indices = np.asarray(b_csr.indices)
    b_values = np.asarray(b_csr.data, dtype=np.float64)

    for row in range(rows):
        for a_pos in range(int(a_indptr[row]), int(a_indptr[row + 1])):
            k_index = int(a_indices[a_pos])
            a_value = a_values[a_pos]
            start = int(b_indptr[k_index])
            stop = int(b_indptr[k_index + 1])
            count = stop - start
            if count:
                out_rows.extend([row] * count)
                out_cols.extend(int(item) for item in b_indices[start:stop])
                out_values.extend(
                    float(a_value * item) for item in b_values[start:stop]
                )

    total = np.zeros((rows, cols), dtype=np.float64)
    if not out_values:
        return total, 0

    row_array = np.asarray(out_rows, dtype=np.int64)
    col_array = np.asarray(out_cols, dtype=np.int64)
    value_array = np.asarray(out_values, dtype=np.float64)
    order = np.lexsort((col_array, row_array))
    row_array = row_array[order]
    col_array = col_array[order]
    value_array = value_array[order]

    if compensated:
        current_row = int(row_array[0])
        current_col = int(col_array[0])
        group_total = float(value_array[0])
        group_compensation = 0.0
        for index in range(1, value_array.size):
            row = int(row_array[index])
            col = int(col_array[index])
            value = float(value_array[index])
            if row != current_row or col != current_col:
                total[current_row, current_col] = group_total
                current_row = row
                current_col = col
                group_total = value
                group_compensation = 0.0
                continue
            corrected = value - group_compensation
            updated = group_total + corrected
            group_compensation = (updated - group_total) - corrected
            group_total = updated
        total[current_row, current_col] = group_total
    else:
        np.add.at(total, (row_array, col_array), value_array)
    return total, int(value_array.size)


def _reduce_partials(
    partials: list[np.ndarray],
    *,
    order: str,
    compensated: bool,
) -> np.ndarray:
    if not partials:
        raise SparseOperatorError(
            E_RESOURCE_LIMIT,
            "no partial matrices were produced",
            phase="execution",
            field="partials",
        )
    if len(partials) == 1:
        return partials[0].copy()
    if order == "pairwise":
        work = [np.asarray(item, dtype=np.float64).copy() for item in partials]
        while len(work) > 1:
            next_level: list[np.ndarray] = []
            for index in range(0, len(work) - 1, 2):
                next_level.append(work[index] + work[index + 1])
            if len(work) % 2:
                next_level.append(work[-1])
            work = next_level
        return work[0]

    ordered = partials if order == "forward" else list(reversed(partials))
    total = np.zeros_like(ordered[0], dtype=np.float64)
    compensation = np.zeros_like(total) if compensated else None
    for partial in ordered:
        if compensation is None:
            total += partial
        else:
            _kahan_add(total, compensation, partial)
    return total


def _stable_reduce_partials(partials: list[np.ndarray]) -> np.ndarray:
    if len(partials) == 1:
        return partials[0].copy()
    return np.sum(
        np.stack(partials, axis=0), axis=0, dtype=np.float64
    )


def _record_position(
    error: np.ndarray,
    *,
    batch_index: int,
    row_offset: int,
    tile: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    if error.size == 0:
        return None
    flat_index = int(np.argmax(error))
    local_row, col = np.unravel_index(flat_index, error.shape)
    payload: dict[str, Any] = {
        "batch": int(batch_index),
        "row": int(row_offset + local_row),
        "col": int(col),
        "abs_error": float(error[local_row, col]),
    }
    if tile is not None:
        payload["row_block_index"] = int(tile[0])
        payload["k_block_index"] = int(tile[1])
    return payload


def execute_batches(
    spec: CaseSpec,
    operands: Operands,
    plan: dict[str, Any],
    config: OperatorConfig,
    *,
    reference: np.ndarray | None = None,
) -> ExecutionOutcome:
    row_block = int(plan["row_block_size"])
    k_block = int(plan["k_block_size"])
    order = str(plan["accumulation_order"])
    compensated = bool(plan["compensated_sum"])
    use_aggregation = bool(plan["use_aggregation_path"])
    local_reverse = plan.get("local_accumulation_order") == "reverse"

    all_partials: list[list[list[np.ndarray]]] = []
    all_block_outputs: list[list[np.ndarray]] = []
    output = np.zeros((spec.batch_size, spec.m, spec.n), dtype=np.float64)
    nnz_processed = 0
    nnz_b_processed = 0
    aggregation_entries = 0
    actual_partial_bytes = 0
    coverage_ok = True
    local_max = 0.0
    local_position: dict[str, Any] | None = None
    cross_max = 0.0
    cross_position: dict[str, Any] | None = None
    envelope_max = 0.0
    envelope_position: dict[str, Any] | None = None
    recompute_max = 0.0

    for batch_index in range(spec.batch_size):
        a_matrix = operands.a[batch_index]
        if spec.kind == "spmm":
            assert operands.b_dense is not None
            rhs_full = np.asarray(operands.b_dense[batch_index], dtype=np.float64)
            b_sparse_full = None
        else:
            assert operands.b_sparse is not None
            b_sparse_full = operands.b_sparse[batch_index]
            rhs_full = np.asarray(b_sparse_full.toarray(), dtype=np.float64)

        batch_partials: list[list[np.ndarray]] = []
        batch_outputs: list[np.ndarray] = []
        nnz_in_batch = 0
        nnz_b_in_batch = 0
        for row_start in range(0, spec.m, row_block):
            row_stop = min(spec.m, row_start + row_block)
            block_partials: list[np.ndarray] = []
            for k_start in range(0, spec.k, k_block):
                k_stop = min(spec.k, k_start + k_block)
                a_tile = a_matrix[row_start:row_stop, k_start:k_stop].tocsr()
                nnz_in_batch += int(a_tile.nnz)
                if spec.kind == "spmm":
                    rhs_tile = rhs_full[k_start:k_stop, :]
                    if row_start == 0:
                        nnz_b_in_batch += int(rhs_tile.size)
                    partial = _local_direct(
                        a_tile,
                        rhs_tile,
                        compensated=compensated,
                        reverse=local_reverse,
                    )
                elif use_aggregation:
                    assert b_sparse_full is not None
                    b_tile = b_sparse_full[k_start:k_stop, :].tocsr()
                    if row_start == 0:
                        nnz_b_in_batch += int(b_tile.nnz)
                    partial, entries = _local_aggregated(
                        a_tile, b_tile, compensated=compensated
                    )
                    aggregation_entries += entries
                    if (
                        entries * config.aggregation_entry_bytes
                        > config.max_aggregation_bytes
                    ):
                        raise SparseOperatorError(
                            E_RESOURCE_LIMIT,
                            "actual aggregation buffer exceeds max_aggregation_bytes",
                            case_id=spec.case_id,
                            phase="execution",
                            field="max_aggregation_bytes",
                            details={
                                "entries": entries,
                                "estimated_bytes": entries
                                * config.aggregation_entry_bytes,
                                "allowed": config.max_aggregation_bytes,
                            },
                        )
                else:
                    rhs_tile = rhs_full[k_start:k_stop, :]
                    assert b_sparse_full is not None
                    if row_start == 0:
                        nnz_b_in_batch += int(
                            b_sparse_full[k_start:k_stop, :].nnz
                        )
                    partial = _local_direct(
                        a_tile,
                        rhs_tile,
                        compensated=compensated,
                        reverse=local_reverse,
                    )

                actual_partial_bytes += int(partial.nbytes)
                block_partials.append(partial)
                if config.diagnose:
                    a_dense_tile = np.asarray(a_tile.toarray(), dtype=np.float64)
                    if spec.kind == "spmm":
                        rhs_dense_tile = np.asarray(rhs_tile, dtype=np.float64)
                    else:
                        assert b_sparse_full is not None
                        rhs_dense_tile = np.asarray(
                            b_sparse_full[k_start:k_stop, :].toarray(),
                            dtype=np.float64,
                        )
                    tile_reference = a_dense_tile @ rhs_dense_tile
                    local_error = np.abs(partial - tile_reference)
                    tile_max = float(local_error.max()) if local_error.size else 0.0
                    if tile_max > local_max:
                        local_max = tile_max
                        local_position = _record_position(
                            local_error,
                            batch_index=batch_index,
                            row_offset=row_start,
                            tile=(
                                row_start // row_block,
                                k_start // k_block,
                            ),
                        )

            block_output = _reduce_partials(
                block_partials, order=order, compensated=compensated
            )
            actual_partial_bytes += int(block_output.nbytes)
            batch_partials.append(block_partials)
            batch_outputs.append(block_output)
            if config.diagnose:
                stable_block = _stable_reduce_partials(block_partials)
                reduction_error = np.abs(block_output - stable_block)
                tile_max = float(reduction_error.max()) if reduction_error.size else 0.0
                if tile_max > cross_max:
                    cross_max = tile_max
                    cross_position = _record_position(
                        reduction_error,
                        batch_index=batch_index,
                        row_offset=row_start,
                    )
                if reference is not None:
                    envelope_error = np.abs(
                        block_output - reference[batch_index, row_start:row_stop, :]
                    )
                    tile_max = float(envelope_error.max()) if envelope_error.size else 0.0
                    if tile_max > envelope_max:
                        envelope_max = tile_max
                        envelope_position = _record_position(
                            envelope_error,
                            batch_index=batch_index,
                            row_offset=row_start,
                        )
            recomputed = _reduce_partials(
                block_partials, order=order, compensated=compensated
            )
            recompute_error = np.max(np.abs(recomputed - block_output))
            recompute_max = max(recompute_max, float(recompute_error))
            output[batch_index, row_start:row_stop, :] = block_output

        nnz_processed += int(a_matrix.nnz)
        nnz_b_processed += nnz_b_in_batch
        if nnz_in_batch != int(a_matrix.nnz):
            coverage_ok = False
        expected_b = (
            spec.k * spec.n
            if spec.kind == "spmm"
            else int(b_sparse_full.nnz)  # type: ignore[union-attr]
        )
        if nnz_b_in_batch != expected_b:
            coverage_ok = False
        all_partials.append(batch_partials)
        all_block_outputs.append(batch_outputs)

    if actual_partial_bytes > config.max_memory_bytes:
        raise SparseOperatorError(
            E_RESOURCE_LIMIT,
            "actual partial buffers exceed max_memory_bytes",
            case_id=spec.case_id,
            phase="execution",
            field="max_memory_bytes",
            details={
                "actual_partial_bytes": actual_partial_bytes,
                "allowed": config.max_memory_bytes,
            },
        )

    return ExecutionOutcome(
        result=output,
        partials=all_partials,
        block_outputs=all_block_outputs,
        nnz_processed=nnz_processed,
        nnz_b_processed=nnz_b_processed,
        aggregation_entries=aggregation_entries,
        actual_partial_bytes=actual_partial_bytes,
        coverage_ok=coverage_ok,
        local_max_abs_error=local_max,
        local_max_position=local_position,
        cross_reduction_max_abs_error=cross_max,
        cross_reduction_max_position=cross_position,
        row_block_envelope_max_abs_error=envelope_max,
        row_block_envelope_position=envelope_position,
        reduction_recompute_max_abs_error=recompute_max,
        stage_methods={
            "row_block": (
                "disjoint row partition; row chunks never share output elements"
            ),
            "cross_block_reduction": (
                f"{order} reduction of k-tile partials; compensated={compensated}"
            ),
            "sparse_index_summation": (
                "aggregated COO grouping"
                if use_aggregation
                else "direct CSR row accumulation"
            ),
        },
    )

