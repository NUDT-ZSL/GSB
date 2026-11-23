"""Deterministic sample aggregation and quantiles."""

from __future__ import annotations

import json
import math
from typing import Any

from .canonical import canonical_json, digest, stable_round


QUANTILES = (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99))


def quantile(sorted_values: list[int], numerator: int, denominator: int = 100) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * numerator / denominator
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    value = sorted_values[lower] + fraction * (
        sorted_values[upper] - sorted_values[lower]
    )
    return stable_round(value)


def build_aggregate(
    *,
    template_id: str,
    snapshot_version: int,
    structure: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda sample: sample["seq"])
    durations = sorted(sample["duration_ms"] for sample in ordered)
    scans = [sample["scanned_rows"] for sample in ordered]
    returned = [sample["returned_rows"] for sample in ordered]
    count = len(ordered)
    window_keys = sorted({sample["window_key"] for sample in ordered})
    core = {
        "schema_version": 1,
        "template_id": template_id,
        "snapshot_version": snapshot_version,
        "structure": structure,
        "sample_count": count,
        "durations": durations,
        "scanned_rows": scans,
        "returned_rows": returned,
        "window_keys": window_keys,
        "min_seq": ordered[0]["seq"],
        "max_seq": ordered[-1]["seq"],
        "min_clock_ms": min(sample["logical_clock_ms"] for sample in ordered),
        "max_clock_ms": max(sample["logical_clock_ms"] for sample in ordered),
        "total_scanned_rows": sum(scans),
        "total_returned_rows": sum(returned),
        "avg_scanned_rows": stable_round(sum(scans) / count),
        "avg_returned_rows": stable_round(sum(returned) / count),
    }
    for label, percentile in QUANTILES:
        core[label] = quantile(durations, percentile)
    core["aggregate_digest"] = digest(core)
    return core


def aggregate_json(aggregate: dict[str, Any]) -> str:
    return canonical_json(aggregate)


def aggregate_from_row(row: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "template_id": row["template_id"],
        "snapshot_version": row["snapshot_version"],
        "structure": json.loads(row["structure_json"]),
        "sample_count": row["sample_count"],
        "durations": json.loads(row["durations_json"]),
        "scanned_rows": json.loads(row["scanned_json"]),
        "returned_rows": json.loads(row["returned_json"]),
        "window_keys": json.loads(row["window_keys_json"]),
        "min_seq": row["min_seq"],
        "max_seq": row["max_seq"],
        "min_clock_ms": row["min_clock_ms"],
        "max_clock_ms": row["max_clock_ms"],
        "total_scanned_rows": row["total_scanned_rows"],
        "total_returned_rows": row["total_returned_rows"],
        "avg_scanned_rows": row["avg_scanned_rows"],
        "avg_returned_rows": row["avg_returned_rows"],
        "p50": row["p50"],
        "p90": row["p90"],
        "p95": row["p95"],
        "p99": row["p99"],
        "aggregate_digest": row["digest"],
    }
