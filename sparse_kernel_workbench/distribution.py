"""Nonzero distribution statistics and direction-aware layout selection."""

from __future__ import annotations

import math
from typing import Any

from .model import NormalizedMatrix
from .spec import OperationSpec


def _axis_summary(counts: list[int], axis_name: str) -> dict[str, Any]:
    total = sum(counts)
    length = len(counts)
    active = [value for value in counts if value > 0]
    active_count = len(active)
    empty_indices = [index for index, value in enumerate(counts) if value == 0]
    mean_all = total / length if length else 0.0
    mean_active = total / active_count if active_count else 0.0
    variance = (
        sum((value - mean_all) ** 2 for value in counts) / length if length else 0.0
    )
    std_all = math.sqrt(variance)
    maximum = max(counts) if counts else 0
    minimum_active = min(active) if active else 0
    sorted_active = sorted(active)
    p90 = (
        sorted_active[min(active_count - 1, math.ceil(active_count * 0.9) - 1)]
        if sorted_active
        else 0
    )
    return {
        "axis": axis_name,
        "counts": counts,
        "vector_length": length,
        "nonzero_total": total,
        "active_axis_count": active_count,
        "empty_axis_count": len(empty_indices),
        "empty_indices": empty_indices,
        "max_nonzeros_in_axis": maximum,
        "max_axis_indices": [
            index for index, value in enumerate(counts) if value == maximum
        ],
        "min_nonzeros_in_active_axis": minimum_active,
        "mean_nonzeros_all_axes": mean_all,
        "mean_nonzeros_active_axes": mean_active,
        "std_nonzeros_all_axes": std_all,
        "coefficient_of_variation": (std_all / mean_all if mean_all else 0.0),
        "active_max_to_mean_ratio": (maximum / mean_active if mean_active else 0.0),
        "p90_nonzeros_in_active_axis": p90,
    }


def nonzero_distribution(matrix: NormalizedMatrix) -> dict[str, Any]:
    rows = _axis_summary(matrix.row_counts(), "row")
    columns = _axis_summary(matrix.column_counts(), "column")
    return {
        "nonzero_count": matrix.nonzero_count,
        "rows": rows,
        "columns": columns,
        "recompute": {
            "row_counts": "row_counts[r] = count(entry.row == r)",
            "column_counts": "column_counts[c] = count(entry.column == c)",
            "empty_rows": "indices where row_counts[index] == 0",
            "empty_columns": "indices where column_counts[index] == 0",
        },
    }


def select_layout(
    operation: OperationSpec,
    distribution: dict[str, Any],
) -> dict[str, Any]:
    row_stats = distribution["rows"]
    column_stats = distribution["columns"]
    if operation.direction == "row_accumulate":
        selected = "csr"
        alternative = "csc"
        primary = row_stats
        secondary = column_stats
        direction_axis = "row"
        rule = (
            "row_accumulate uses CSR because output-row accumulators can consume "
            "row-contiguous storage and complete each row before a batch boundary"
        )
    else:
        selected = "csc"
        alternative = "csr"
        primary = column_stats
        secondary = row_stats
        direction_axis = "column"
        rule = (
            "column_accumulate uses CSC because output-column access can consume "
            "column-contiguous storage; batching still preserves complete rows"
        )

    rationale = [
        rule,
        (
            f"operation direction {operation.direction} names the {direction_axis} "
            "axis as the primary accumulator"
        ),
        (
            f"{direction_axis} distribution: total={primary['nonzero_total']}, "
            f"max={primary['max_nonzeros_in_axis']} at "
            f"{primary['max_axis_indices']}, active={primary['active_axis_count']}, "
            f"empty={primary['empty_axis_count']}, "
            f"active_max_to_mean_ratio={primary['active_max_to_mean_ratio']:.6f}"
        ),
        (
            f"cross-axis distribution: total={secondary['nonzero_total']}, "
            f"max={secondary['max_nonzeros_in_axis']} at "
            f"{secondary['max_axis_indices']}, active={secondary['active_axis_count']}, "
            f"empty={secondary['empty_axis_count']}, "
            f"active_max_to_mean_ratio={secondary['active_max_to_mean_ratio']:.6f}"
        ),
        (
            f"{alternative.upper()} remains an executed comparison candidate; "
            "selection is derived from the declared direction and distribution, not fixed"
        ),
    ]
    return {
        "direction": operation.direction,
        "selected": selected,
        "alternative": alternative,
        "primary_axis": direction_axis,
        "decision_rule": "direction_axis_storage_locality_with_skew_evidence",
        "row_distribution": {
            "max_nonzeros_in_axis": row_stats["max_nonzeros_in_axis"],
            "empty_axis_count": row_stats["empty_axis_count"],
            "active_max_to_mean_ratio": row_stats["active_max_to_mean_ratio"],
            "coefficient_of_variation": row_stats["coefficient_of_variation"],
        },
        "column_distribution": {
            "max_nonzeros_in_axis": column_stats["max_nonzeros_in_axis"],
            "empty_axis_count": column_stats["empty_axis_count"],
            "active_max_to_mean_ratio": column_stats["active_max_to_mean_ratio"],
            "coefficient_of_variation": column_stats["coefficient_of_variation"],
        },
        "rationale": rationale,
    }
