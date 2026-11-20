from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def _label_span(rows: Iterable[Mapping[str, Any]]) -> int:
    return len({str(row["pool_label"]) for row in rows})


def estimate_costs(
    idle_rows: Iterable[Mapping[str, Any]],
    *,
    queue_depth: int,
    bucket_width_ms: int,
    now_ms: int,
    age_tolerance_ms: int = 0,
) -> dict[str, Any]:
    rows = list(idle_rows)
    idle_count = len(rows)
    label_span = _label_span(rows)
    occupied_buckets = len(
        {
            (
                str(row["pool_label"]),
                max(
                    0,
                    now_ms - int(row["idle_since_ms"]) - age_tolerance_ms,
                )
                // bucket_width_ms,
            )
            for row in rows
        }
    )
    exact = (
        idle_count * math.log2(idle_count + 2)
        + queue_depth * 0.5
        + label_span * 0.75
    )
    tiered = (
        occupied_buckets
        + idle_count * 0.2
        + queue_depth * 0.25
        + label_span * 0.4
    )
    return {
        "idle_count": idle_count,
        "queue_depth": queue_depth,
        "pool_label_span": label_span,
        "occupied_buckets": occupied_buckets,
        "exact_cost": round(exact, 6),
        "tiered_cost": round(tiered, 6),
    }


def decide_mode(
    current_mode: str,
    costs: Mapping[str, Any],
    *,
    margin_ratio: float,
    tiered_min_idle: int,
) -> tuple[str, str | None]:
    """Return (mode, reason). An unchanged mode has reason None."""
    idle_count = int(costs["idle_count"])
    exact_cost = float(costs["exact_cost"])
    tiered_cost = float(costs["tiered_cost"])
    if idle_count < tiered_min_idle:
        if current_mode == "tiered":
            return (
                "exact",
                "idle_count below tiered_min_idle; exact queue avoids sparse bucket scans",
            )
        return "exact", None
    if current_mode == "exact":
        if tiered_cost < exact_cost * (1.0 - margin_ratio):
            return (
                "tiered",
                (
                    f"tiered_cost {tiered_cost:.6f} < exact_cost {exact_cost:.6f} "
                    f"* (1 - {margin_ratio:.6f}) and idle_count >= {tiered_min_idle}"
                ),
            )
        return "exact", None
    if current_mode == "tiered":
        if exact_cost < tiered_cost * (1.0 - margin_ratio):
            return (
                "exact",
                (
                    f"exact_cost {exact_cost:.6f} < tiered_cost {tiered_cost:.6f} "
                    f"* (1 - {margin_ratio:.6f})"
                ),
            )
        return "tiered", None
    return "exact", "unknown mode reset to exact"


def order_idle(
    idle_rows: Iterable[Mapping[str, Any]],
    *,
    now_ms: int,
    mode: str,
    bucket_width_ms: int,
) -> list[Mapping[str, Any]]:
    """Order rows oldest-first.

    Tiered buckets are traversed from oldest bucket to newest, and rows inside a
    bucket retain the exact idle-age order. This makes the selected connection
    sequence bit-for-bit identical between exact and tiered modes.
    """

    rows = list(idle_rows)
    if mode == "tiered":
        return sorted(
            rows,
            key=lambda row: (
                -((now_ms - int(row["idle_since_ms"])) // bucket_width_ms),
                int(row["idle_since_ms"]),
                str(row["endpoint_name"]),
                str(row["conn_id"]),
            ),
        )
    return sorted(
        rows,
        key=lambda row: (
            int(row["idle_since_ms"]),
            str(row["endpoint_name"]),
            str(row["conn_id"]),
        ),
    )


def idle_tiers(
    idle_rows: Iterable[Mapping[str, Any]],
    *,
    now_ms: int,
    bucket_width_ms: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in idle_rows:
        age = now_ms - int(row["idle_since_ms"])
        key = (str(row["pool_label"]), age // bucket_width_ms)
        grouped.setdefault(key, []).append(row)
    tiers: list[dict[str, Any]] = []
    for (label, bucket_index), rows in sorted(
        grouped.items(), key=lambda item: (item[0][0], -item[0][1])
    ):
        ordered = sorted(
            rows,
            key=lambda row: (
                int(row["idle_since_ms"]),
                str(row["endpoint_name"]),
                str(row["conn_id"]),
            ),
        )
        tiers.append(
            {
                "pool_label": label,
                "bucket_index": bucket_index,
                "age_range_ms": [
                    bucket_index * bucket_width_ms,
                    (bucket_index + 1) * bucket_width_ms - 1,
                ],
                "connection_ids": [str(row["conn_id"]) for row in ordered],
            }
        )
    return tiers
