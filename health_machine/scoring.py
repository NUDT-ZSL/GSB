"""Pure deterministic scoring and injected-clock window helpers."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Any


def window_index_for_clock(clock_ms: int, window_ms: int) -> int:
    if clock_ms < 0:
        raise ValueError("clock_ms must be non-negative")
    return clock_ms // window_ms


def window_bounds(window_index: int, window_ms: int) -> tuple[int, int]:
    start = window_index * window_ms
    return start, start + window_ms


def level_score(
    *,
    successes: int,
    failures: int,
    avg_latency_ms: Decimal,
    weight: int,
    min_score_bp: int,
    latency_budget_ms: Decimal,
) -> dict[str, Any]:
    total = successes + failures
    availability_bp = (successes * 10000) // total if total > 0 else 0
    if avg_latency_ms <= latency_budget_ms:
        latency_score_bp = 10000
    elif latency_budget_ms <= 0:
        latency_score_bp = 0
    else:
        latency_score_bp = int(
            ((Decimal(10000) * latency_budget_ms) / avg_latency_ms).to_integral_value(rounding=ROUND_FLOOR)
        )
    score_bp = (70 * availability_bp + 30 * latency_score_bp) // 100
    return {
        "score_bp": score_bp,
        "availability_bp": availability_bp,
        "latency_score_bp": latency_score_bp,
        "total_probes": total,
        "score_breach": score_bp < min_score_bp,
        "weight": weight,
        "min_score_bp": min_score_bp,
    }


def combine_window_scores(level_results: list[dict[str, Any]]) -> int:
    total_weight = sum(item["weight"] for item in level_results)
    if total_weight <= 0:
        return 0
    return sum(item["weight"] * item["score_bp"] for item in level_results) // total_weight


def timeout_breached(previous_clock_ms: int | None, current_clock_ms: int, timeout_ms: int) -> bool:
    if previous_clock_ms is None:
        return False
    return current_clock_ms - previous_clock_ms > timeout_ms
