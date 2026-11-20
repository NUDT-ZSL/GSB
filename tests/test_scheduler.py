from __future__ import annotations

import random

from dbpool.scheduler import decide_mode, estimate_costs, order_idle


def _rows(count: int) -> list[dict[str, object]]:
    rng = random.Random(42)
    rows = []
    for index in range(count):
        rows.append(
            {
                "conn_id": f"conn-{index:04d}",
                "tenant": f"tenant-{index % 5}",
                "pool_label": ["read", "write", "analytics"][index % 3],
                "endpoint_name": f"endpoint-{index % 2}",
                "idle_since_ms": rng.randrange(0, 500),
                "generation": 1,
            }
        )
    return rows


def test_exact_and_tiered_order_are_identical() -> None:
    rows = _rows(200)
    exact = [
        row["conn_id"]
        for row in order_idle(
            rows,
            now_ms=500,
            mode="exact",
            bucket_width_ms=10,
        )
    ]
    tiered = [
        row["conn_id"]
        for row in order_idle(
            rows,
            now_ms=500,
            mode="tiered",
            bucket_width_ms=10,
        )
    ]
    assert exact == tiered


def test_switch_uses_cost_margin_and_hysteresis() -> None:
    rows = _rows(12)
    costs = estimate_costs(
        rows,
        queue_depth=2,
        bucket_width_ms=10,
        now_ms=500,
    )
    assert costs["exact_cost"] > costs["tiered_cost"]
    mode, reason = decide_mode(
        "exact",
        costs,
        margin_ratio=0.15,
        tiered_min_idle=3,
    )
    assert mode == "tiered"
    assert reason is not None

    low = estimate_costs(
        rows[:2],
        queue_depth=0,
        bucket_width_ms=10,
        now_ms=500,
    )
    mode, reason = decide_mode(
        "tiered",
        low,
        margin_ratio=0.15,
        tiered_min_idle=3,
    )
    assert mode == "exact"
    assert reason is not None
