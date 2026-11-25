"""Global deterministic bin boundary calculation and bin assignment."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .hashing import canonical_hash


class ComparisonMeter:
    """Counts explicit boundary comparisons for resource diagnostics."""

    def __init__(self, initial: int = 0) -> None:
        self.count = int(initial)

    def compare_gt(self, value: float, boundary: float) -> bool:
        self.count += 1
        return value > boundary

    def add(self, amount: int) -> None:
        self.count += int(amount)

    def to_dict(self) -> Dict[str, int]:
        return {"comparison_count": self.count}


def normalize_float(value: float) -> float:
    value = float(value)
    if value == 0.0:
        return 0.0
    return value


def _unique_ordered(values: Sequence[float]) -> List[float]:
    result: List[float] = []
    for value in values:
        value = normalize_float(value)
        if not result or value != result[-1]:
            result.append(value)
    return result


def linear_quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    if probability <= 0.0:
        return normalize_float(sorted_values[0])
    if probability >= 1.0:
        return normalize_float(sorted_values[-1])
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return normalize_float(sorted_values[lower_index])
    fraction = position - lower_index
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return normalize_float(lower + fraction * (upper - lower))


def equal_width_boundaries(values: Sequence[float], bin_count: int) -> Tuple[float, float, List[float]]:
    low = normalize_float(min(values))
    high = normalize_float(max(values))
    if low == high:
        return low, high, []
    raw: List[float] = []
    for index in range(1, bin_count):
        boundary = low + (high - low) * (index / bin_count)
        raw.append(normalize_float(boundary))
    return low, high, _unique_ordered(raw)


def equal_freq_boundaries(values: Sequence[float], bin_count: int) -> Tuple[float, float, List[float]]:
    ordered = sorted(normalize_float(value) for value in values)
    low = ordered[0]
    high = ordered[-1]
    raw = [
        linear_quantile(ordered, index / bin_count)
        for index in range(1, bin_count)
    ]
    return low, high, _unique_ordered(raw)


def compute_boundaries(
    rows: Sequence[Mapping[str, Any]], config: Mapping[str, Any]
) -> Dict[str, Any]:
    columns: Dict[str, Any] = {}
    tie_decisions: List[Dict[str, str]] = []
    previous_signature = None

    for column in config["numeric_columns"]:
        spec = config["column_specs"][column]
        bin_count = int(spec["bin_count"])
        method = spec["bin_method"]
        values = [float(row[column]) for row in rows]
        if method == "equal_width":
            low, high, internal = equal_width_boundaries(values, bin_count)
        elif method == "equal_freq":
            low, high, internal = equal_freq_boundaries(values, bin_count)
        else:  # config validation prevents this branch
            raise ValueError(f"unsupported bin_method: {method}")

        signature = canonical_hash({"method": method, "boundaries": internal})
        if previous_signature == signature:
            tie_decisions.append(
                {
                    "rule": "identical_boundary_signature",
                    "selected_by": "column_name_lexicographic",
                    "column": column,
                }
            )
        previous_signature = signature

        columns[column] = {
            "bin_count_requested": bin_count,
            "bin_count_effective": len(internal) + 1,
            "bin_method": method,
            "min": low,
            "max": high,
            "boundaries": internal,
            "assignment_rule": "scan_boundaries_and_keep_equal_value_in_lower_bin",
        }

    result = {
        "column_order": list(config["numeric_columns"]),
        "tie_break_rule": "column_name_lexicographic_ascending",
        "tie_decisions": tie_decisions,
        "columns": columns,
    }
    result["boundary_hash"] = canonical_hash(
        {key: value for key, value in result.items() if key != "boundary_hash"}
    )
    return result


def assign_bin(
    value: float,
    boundaries: Sequence[float],
    meter: Optional[ComparisonMeter] = None,
) -> int:
    index = 0
    for boundary in boundaries:
        if meter is not None:
            is_greater = meter.compare_gt(float(value), float(boundary))
        else:
            is_greater = float(value) > float(boundary)
        if is_greater:
            index += 1
        else:
            break
    return index


def assign_row_bins(
    row: Mapping[str, Any],
    boundary_result: Mapping[str, Any],
    config: Mapping[str, Any],
    meter: Optional[ComparisonMeter] = None,
) -> Dict[str, int]:
    bins: Dict[str, int] = {}
    for column in config["numeric_columns"]:
        column_boundaries = boundary_result["columns"][column]["boundaries"]
        bins[column] = assign_bin(float(row[column]), column_boundaries, meter)
    return bins


def estimate_boundary_comparisons(row_count: int, column_count: int, bin_count_sum: int) -> int:
    """Deterministic conservative comparison estimate for resource planning."""

    if row_count <= 1:
        sort_comparisons = 0
    else:
        log_rows = max(1, math.ceil(math.log2(row_count)))
        sort_comparisons = row_count * log_rows
    assignment = row_count * max(1, bin_count_sum)
    return column_count * sort_comparisons + assignment
