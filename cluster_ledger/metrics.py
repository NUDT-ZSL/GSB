"""Integer fixed-point latency statistics with explicit quantization bounds."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from .clock import (
    LATENCY_CALIBER_VERSION,
    LATENCY_FIXED_SCALE,
    LATENCY_QUANTIZATION_ERROR_MS,
    fixed_ms_from_ns,
)


def _p95_nearest_rank(sorted_values: list[int]) -> int:
    if not sorted_values:
        return 0
    index = (95 * len(sorted_values) + 99) // 100 - 1
    return sorted_values[max(0, index)]


def _group_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    fixed = [fixed_ms_from_ns(max(0, int(row["ended_mono_ns"]) - int(row["started_mono_ns"]))) for row in rows]
    fixed.sort()
    count = len(fixed)
    mean_fixed = sum(fixed) // count if count else 0
    p95_fixed = _p95_nearest_rank(fixed)
    maximum_fixed = fixed[-1] if fixed else 0
    return {
        "count": count,
        "mean_fixed": mean_fixed,
        "p95_fixed": p95_fixed,
        "max_fixed": maximum_fixed,
        "over_budget_count": sum(1 for row in rows if bool(row.get("exceeded_budget", False))),
    }


def _groups(rows: Iterable[Mapping[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return {key: _group_stats(grouped[key]) for key in sorted(grouped)}


def compute_latency_stats(interactions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate interaction latency by kind, task, cluster, and task+cluster.

    ``fixed`` values use ``LATENCY_FIXED_SCALE=1000`` units per millisecond,
    i.e. integer microseconds expressed as thousandths of a millisecond.
    """

    rows = [dict(row) for row in interactions]
    for row in rows:
        row["task_id"] = row.get("task_id") or "*"
        row["cluster_id"] = row.get("cluster_id") or "*"
    all_stats = _group_stats(rows)
    report = {
        "caliber_version": LATENCY_CALIBER_VERSION,
        "fixed_scale": LATENCY_FIXED_SCALE,
        "fixed_unit": "1/1000 ms (one microsecond)",
        "quantization_error_upper_bound_ms": LATENCY_QUANTIZATION_ERROR_MS,
        "quantization_error_upper_bound_fixed": 1,
        "selection_method": "nearest-rank P95; no interpolation",
        "budget_events_preserved": True,
        "overall": all_stats,
        "by_kind": _groups(rows, lambda row: str(row.get("kind", "*"))),
        "by_task": _groups(rows, lambda row: str(row["task_id"])),
        "by_cluster": _groups(rows, lambda row: str(row["cluster_id"])),
        "by_task_cluster": _groups(
            rows, lambda row: f"{row['task_id']}|{row['cluster_id']}"
        ),
        "over_budget_events": [
            row for row in rows if bool(row.get("exceeded_budget", False))
        ],
    }
    return report

