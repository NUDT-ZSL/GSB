from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from .util import rounded_decimal, rounded_number, sha256_json


def percentile(sorted_values: list[int], fraction: Decimal) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    fraction = fraction if isinstance(fraction, Decimal) else Decimal(str(fraction))
    if len(sorted_values) == 1:
        return rounded_number(sorted_values[0])
    position = Decimal(len(sorted_values) - 1) * fraction
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    lower = Decimal(sorted_values[lower_index])
    upper = Decimal(sorted_values[upper_index])
    value = lower + (upper - lower) * (position - Decimal(lower_index))
    return rounded_number(value)


def aggregate_samples(samples: Iterable[dict[str, Any]], template_id: str, template_sql: str) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda sample: sample["sample_seq"])
    if not ordered:
        raise ValueError("aggregate requires at least one sample")
    count = len(ordered)
    durations = sorted(sample["duration_ms"] for sample in ordered)
    total_duration = sum(sample["duration_ms"] for sample in ordered)
    total_scanned = sum(sample["rows_scanned"] for sample in ordered)
    total_returned = sum(sample["rows_returned"] for sample in ordered)
    zero_return_count = sum(1 for sample in ordered if sample["rows_returned"] == 0)
    window_counts: dict[int, int] = {}
    for sample in ordered:
        window_start = sample["window_start_ms"]
        window_counts[window_start] = window_counts.get(window_start, 0) + 1
    windows = [
        {"window_start_ms": window_start, "sample_count": window_counts[window_start]}
        for window_start in sorted(window_counts)
    ]
    scanned_mean = rounded_decimal(Decimal(total_scanned) / Decimal(count))
    returned_mean = rounded_decimal(Decimal(total_returned) / Decimal(count))
    amplification = (
        rounded_decimal(Decimal(total_scanned) / Decimal(total_returned)) if total_returned > 0 else None
    )
    sample_digest = sha256_json(
        [
            {
                "sample_seq": sample["sample_seq"],
                "sample_digest": sample.get("sample_digest"),
            }
            for sample in ordered
        ]
    )
    payload = {
        "template_id": template_id,
        "template_sql": template_sql,
        "sample_count": count,
        "sample_seq_min": ordered[0]["sample_seq"],
        "sample_seq_max": ordered[-1]["sample_seq"],
        "sample_ids_digest": sample_digest,
        "latency_ms": {
            "p50": percentile(durations, Decimal("0.50")),
            "p95": percentile(durations, Decimal("0.95")),
            "min": durations[0],
            "max": durations[-1],
            "mean": rounded_number(Decimal(total_duration) / Decimal(count)),
        },
        "scanned_rows": {
            "mean": float(scanned_mean),
            "total": total_scanned,
        },
        "returned_rows": {
            "mean": float(returned_mean),
            "total": total_returned,
            "zero_return_samples": zero_return_count,
        },
        "scan_amplification": float(amplification) if amplification is not None else None,
        "windows": windows,
    }
    payload["aggregate_version"] = "agg_" + sha256_json(payload)[:20]
    return payload
