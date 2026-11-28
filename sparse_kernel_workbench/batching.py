"""Row-safe batching with layout-specific scan ranges and ownership checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import WorkbenchError
from .model import NormalizedMatrix
from .spec import OperationSpec


@dataclass(frozen=True)
class BatchPlan:
    batch_id: int
    row_start: int
    row_end: int
    canonical_nnz_start: int
    canonical_nnz_end: int
    scan_entry_ids: tuple[int, ...]
    empty_rows: tuple[int, ...]
    over_budget: bool
    reason: str | None
    storage_scan_ranges: tuple[tuple[int, int], ...]

    @property
    def nonzero_count(self) -> int:
        return self.canonical_nnz_end - self.canonical_nnz_start


@dataclass(frozen=True)
class BatchingPlan:
    layout: str
    scan_order: str
    batch_budget: int
    batches: tuple[BatchPlan, ...]
    empty_rows: tuple[int, ...]
    empty_columns: tuple[int, ...]
    row_counts: tuple[int, ...]
    column_counts: tuple[int, ...]
    row_ownership: tuple[int, ...]
    partition_rule: str


def _partition_rows(row_counts: list[int], budget: int) -> list[tuple[int, int, bool, str | None]]:
    """Partition all rows into contiguous spans without splitting one row."""

    batches: list[tuple[int, int, bool, str | None]] = []
    start = 0
    current_nnz = 0
    rows = len(row_counts)
    for row, count in enumerate(row_counts):
        if count > budget:
            if row > start and current_nnz > 0:
                batches.append((start, row, False, None))
                start = row
                current_nnz = 0
            batches.append((start, row + 1, True, "single_row_exceeds_batch_budget_kept_whole"))
            start = row + 1
            current_nnz = 0
            continue
        if current_nnz > 0 and current_nnz + count > budget:
            batches.append((start, row, False, None))
            start = row
            current_nnz = 0
        current_nnz += count
    if start < rows:
        batches.append((start, rows, False, None))
    return batches


def _ranges_from_positions(positions: list[int]) -> tuple[tuple[int, int], ...]:
    if not positions:
        return ()
    positions.sort()
    ranges: list[tuple[int, int]] = []
    start = positions[0]
    previous = positions[0]
    for position in positions[1:]:
        if position == previous + 1:
            previous = position
            continue
        ranges.append((start, previous + 1))
        start = position
        previous = position
    ranges.append((start, previous + 1))
    return tuple(ranges)


def build_batching_plan(
    matrix: NormalizedMatrix,
    operation: OperationSpec,
    layout: str,
) -> BatchingPlan:
    if layout not in ("csr", "csc"):
        raise WorkbenchError(
            "SKW-KERNEL-001",
            f"Unsupported layout: {layout}",
            stage="batching",
            context={"version": matrix.version, "field": "layout", "element": "strategy"},
        )
    row_counts = matrix.row_counts()
    column_counts = matrix.column_counts()
    spans = _partition_rows(row_counts, operation.batch_budget)

    row_prefix = [0]
    for count in row_counts:
        row_prefix.append(row_prefix[-1] + count)

    if layout == "csr":
        scan_order = "row_major"
        csc_positions = None
    else:
        scan_order = "column_major_within_row_batch"
        csc_id_order = sorted(
            range(matrix.nonzero_count),
            key=lambda entry_id: (
                matrix.entries[entry_id].column,
                matrix.entries[entry_id].row,
            ),
        )
        csc_positions = {
            entry_id: position for position, entry_id in enumerate(csc_id_order)
        }

    batches: list[BatchPlan] = []
    row_ownership = [0] * matrix.rows
    for batch_id, (row_start, row_end, over_budget, reason) in enumerate(spans):
        canonical_start = row_prefix[row_start]
        canonical_end = row_prefix[row_end]
        canonical_ids = tuple(range(canonical_start, canonical_end))
        if layout == "csr":
            scan_ids = canonical_ids
            storage_ranges = ((canonical_start, canonical_end),)
        else:
            assert csc_positions is not None
            scan_ids = tuple(
                sorted(
                    canonical_ids,
                    key=lambda entry_id: (
                        matrix.entries[entry_id].column,
                        matrix.entries[entry_id].row,
                    ),
                )
            )
            positions = [csc_positions[entry_id] for entry_id in canonical_ids]
            storage_ranges = _ranges_from_positions(positions)
        batch_empty_rows = tuple(
            row for row in range(row_start, row_end) if row_counts[row] == 0
        )
        for row in range(row_start, row_end):
            row_ownership[row] += 1
        batches.append(
            BatchPlan(
                batch_id=batch_id,
                row_start=row_start,
                row_end=row_end,
                canonical_nnz_start=canonical_start,
                canonical_nnz_end=canonical_end,
                scan_entry_ids=scan_ids,
                empty_rows=batch_empty_rows,
                over_budget=over_budget,
                reason=reason,
                storage_scan_ranges=storage_ranges,
            )
        )

    if any(count != 1 for count in row_ownership):
        raise WorkbenchError(
            "SKW-BATCH-001",
            "Row ownership is not exclusive in the batching plan",
            stage="batching",
            context={
                "version": matrix.version,
                "layout": layout,
                "field": "row_ownership",
                "element": "batching_plan",
            },
        )
    assigned = [entry_id for batch in batches for entry_id in batch.scan_entry_ids]
    if len(assigned) != matrix.nonzero_count or len(set(assigned)) != matrix.nonzero_count:
        raise WorkbenchError(
            "SKW-BATCH-002",
            "Batches do not assign every canonical nonzero exactly once",
            stage="batching",
            context={
                "version": matrix.version,
                "layout": layout,
                "field": "scan_entry_ids",
                "element": "batching_plan",
                "assigned": len(assigned),
                "expected": matrix.nonzero_count,
            },
        )

    return BatchingPlan(
        layout=layout,
        scan_order=scan_order,
        batch_budget=operation.batch_budget,
        batches=tuple(batches),
        empty_rows=tuple(
            row for row, count in enumerate(row_counts) if count == 0
        ),
        empty_columns=tuple(
            column for column, count in enumerate(column_counts) if count == 0
        ),
        row_counts=tuple(row_counts),
        column_counts=tuple(column_counts),
        row_ownership=tuple(row_ownership),
        partition_rule=(
            "contiguous_complete_rows; a row wider than the budget is isolated "
            "and kept whole; empty rows stay in their row span"
        ),
    )


def batching_report(plan: BatchingPlan) -> dict[str, Any]:
    return {
        "layout": plan.layout,
        "scan_order": plan.scan_order,
        "batch_nonzero_budget": plan.batch_budget,
        "partition_rule": plan.partition_rule,
        "batch_count": len(plan.batches),
        "empty_rows": list(plan.empty_rows),
        "empty_columns": list(plan.empty_columns),
        "row_integrity": {
            "every_row_in_exactly_one_batch": all(count == 1 for count in plan.row_ownership),
            "dense_rows_split": False,
            "empty_rows_explicit_count": len(plan.empty_rows),
        },
        "batches": [
            {
                "batch_id": batch.batch_id,
                "row_interval": [batch.row_start, batch.row_end],
                "row_interval_convention": "[start, end)",
                "canonical_nonzero_interval": [
                    batch.canonical_nnz_start,
                    batch.canonical_nnz_end,
                ],
                "nonzero_interval_convention": "[start, end) in row-major canonical order",
                "nonzero_count": batch.nonzero_count,
                "storage_scan_ranges": [
                    [start, end] for start, end in batch.storage_scan_ranges
                ],
                "empty_rows": list(batch.empty_rows),
                "over_budget": batch.over_budget,
                "reason": batch.reason,
                "rows_owned": [batch.row_start, batch.row_end],
            }
            for batch in plan.batches
        ],
    }
