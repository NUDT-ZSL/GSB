"""Deterministic sparse matrix by dense vector batch kernel and ledger."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from .batching import BatchingPlan
from .errors import WorkbenchError
from .model import NormalizedMatrix, decimal_text
from .spec import OperationSpec


def execute_kernel(
    matrix: NormalizedMatrix,
    operation: OperationSpec,
    plan: BatchingPlan,
) -> dict[str, Any]:
    """Execute all batches and return a self-contained kernel ledger."""

    result = [0.0] * matrix.rows
    ownership_counts = [0] * matrix.rows
    batch_ledger: list[dict[str, Any]] = []
    scanned_entries: list[int] = []

    for batch in plan.batches:
        row_contributions: dict[int, list[float]] = {}
        chunk_accesses: dict[int, int] = {}
        loaded_chunks: set[int] = set()
        dense_chunk_hits = 0
        dense_chunk_misses = 0

        for entry_id in batch.scan_entry_ids:
            entry = matrix.entries[entry_id]
            scanned_entries.append(entry_id)
            chunk_id = entry.column // operation.dense_chunk_size
            chunk_accesses[chunk_id] = chunk_accesses.get(chunk_id, 0) + 1
            if chunk_id in loaded_chunks:
                dense_chunk_hits += 1
            else:
                loaded_chunks.add(chunk_id)
                dense_chunk_misses += 1
            product = float(entry.value) * matrix.vector.values[entry.column]
            if not math.isfinite(product):
                raise WorkbenchError(
                    "SKW-KERNEL-002",
                    "Non-finite product encountered in sparse kernel",
                    stage="kernel",
                    context={
                        "version": matrix.version,
                        "row": entry.row,
                        "column": entry.column,
                        "field": matrix.fields.get("value", "value"),
                        "element": entry.source_element,
                    },
                )
            row_contributions.setdefault(entry.row, []).append(product)

        increments: list[dict[str, Any]] = []
        for row in range(batch.row_start, batch.row_end):
            contributions = row_contributions.get(row, [])
            delta = math.fsum(contributions) if contributions else 0.0
            if not math.isfinite(delta):
                raise WorkbenchError(
                    "SKW-KERNEL-003",
                    "Non-finite result increment encountered in sparse kernel",
                    stage="kernel",
                    context={
                        "version": matrix.version,
                        "row": row,
                        "element": "result_increment",
                        "field": "result_vector",
                    },
                )
            result[row] += delta
            ownership_counts[row] += 1
            if delta != 0.0:
                increments.append({"row": row, "delta": delta})

        chunk_table = []
        for chunk_id in sorted(chunk_accesses):
            accesses = chunk_accesses[chunk_id]
            chunk_start = chunk_id * operation.dense_chunk_size
            chunk_end = min(chunk_start + operation.dense_chunk_size, matrix.columns)
            chunk_table.append(
                {
                    "chunk_id": chunk_id,
                    "column_interval": [chunk_start, chunk_end],
                    "accesses": accesses,
                    "loads": 1,
                    "hits": accesses - 1,
                }
            )

        batch_ledger.append(
            {
                "batch_id": batch.batch_id,
                "rows_owned": [batch.row_start, batch.row_end],
                "canonical_nonzero_interval": [
                    batch.canonical_nnz_start,
                    batch.canonical_nnz_end,
                ],
                "scanned_nonzero_count": len(batch.scan_entry_ids),
                "scan_order": plan.scan_order,
                "storage_scan_ranges": [
                    [start, end] for start, end in batch.storage_scan_ranges
                ],
                "dense_chunk_hits": dense_chunk_hits,
                "dense_chunk_misses": dense_chunk_misses,
                "dense_chunk_accesses": sum(chunk_accesses.values()),
                "dense_vector_chunk_reuse": chunk_table,
                "result_increment_count": len(increments),
                "result_increments": increments,
                "result_increment_l1": math.fsum(
                    abs(item["delta"]) for item in increments
                ),
                "empty_rows": list(batch.empty_rows),
                "over_budget": batch.over_budget,
                "reason": batch.reason,
            }
        )

    if len(scanned_entries) != matrix.nonzero_count:
        raise WorkbenchError(
            "SKW-KERNEL-004",
            "Kernel scan count does not equal canonical nonzero count",
            stage="kernel",
            context={
                "version": matrix.version,
                "layout": plan.layout,
                "scanned": len(scanned_entries),
                "expected": matrix.nonzero_count,
            },
        )
    if len(set(scanned_entries)) != matrix.nonzero_count:
        raise WorkbenchError(
            "SKW-KERNEL-005",
            "Kernel scanned a canonical nonzero more than once",
            stage="kernel",
            context={
                "version": matrix.version,
                "layout": plan.layout,
                "unique_scanned": len(set(scanned_entries)),
                "expected": matrix.nonzero_count,
            },
        )
    if any(count != 1 for count in ownership_counts):
        raise WorkbenchError(
            "SKW-KERNEL-006",
            "A result element received contributions from more than one batch",
            stage="kernel",
            context={
                "version": matrix.version,
                "layout": plan.layout,
                "duplicate_result_rows": [
                    row for row, count in enumerate(ownership_counts) if count != 1
                ],
            },
        )

    total_scanned = len(scanned_entries)
    batch_count = len(batch_ledger)
    mean_per_batch = Decimal(total_scanned) / Decimal(batch_count) if batch_count else Decimal(0)
    threshold = mean_per_batch * operation.hotspot_multiplier
    for item in batch_ledger:
        hotspot = Decimal(item["scanned_nonzero_count"]) > threshold
        item["hotspot"] = hotspot
        item["hotspot_threshold"] = decimal_text(threshold)
    hotspot_batches = [item for item in batch_ledger if item["hotspot"]]
    peak = max((item["scanned_nonzero_count"] for item in batch_ledger), default=0)

    ledger = {
        "layout": plan.layout,
        "scan_order": plan.scan_order,
        "nonzero_scan_total": total_scanned,
        "batch_count": batch_count,
        "result_vector_length": matrix.rows,
        "mean_nonzeros_per_result_element": (
            total_scanned / matrix.rows if matrix.rows else 0.0
        ),
        "mean_nonzeros_per_batch": float(mean_per_batch),
        "hotspot_average_basis": operation.hotspot_average_basis,
        "hotspot_multiplier": decimal_text(operation.hotspot_multiplier),
        "hotspot_threshold": decimal_text(threshold),
        "hotspot_batch_ids": [item["batch_id"] for item in hotspot_batches],
        "hotspot_batch_count": len(hotspot_batches),
        "hotspot_scan_total": sum(
            item["scanned_nonzero_count"] for item in hotspot_batches
        ),
        "peak_batch_nonzeros": peak,
        "dense_chunk_hits": sum(item["dense_chunk_hits"] for item in batch_ledger),
        "dense_chunk_misses": sum(item["dense_chunk_misses"] for item in batch_ledger),
        "result_increment_total": sum(
            item["result_increment_count"] for item in batch_ledger
        ),
        "scan_accounting": {
            "scanned_nonzero_total": total_scanned,
            "unique_canonical_nonzeros": len(set(scanned_entries)),
            "duplicate_scans": len(scanned_entries) - len(set(scanned_entries)),
            "missing_scans": matrix.nonzero_count - len(set(scanned_entries)),
            "every_canonical_nonzero_scanned_once": (
                len(scanned_entries) == matrix.nonzero_count
                and len(set(scanned_entries)) == matrix.nonzero_count
            ),
        },
        "result_ownership": {
            "batch_count_by_result_element": ownership_counts,
            "every_result_element_owned_by_exactly_one_batch": all(
                count == 1 for count in ownership_counts
            ),
        },
        "batches": batch_ledger,
    }
    return {
        "result_vector": result,
        "ledger": ledger,
    }
