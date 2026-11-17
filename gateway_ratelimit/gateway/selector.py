"""Deterministic adaptive selection of the default rate-limit path."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .models import (
    LIMIT_PATHS,
    PATH_TOKEN_BUCKET,
    PATH_WINDOW_COUNTER,
    PATH_WINDOW_LOG,
    Route,
)

# When the per-second workload estimate of two candidate paths differs by less
# than this absolute amount, the stable tie-break below decides the winner.
WORKLOAD_EPSILON = 500.0


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def estimate_workloads(
    route: Route, client_count: int, span_ms: int, observed_max_log: int
) -> dict[str, float]:
    """Return estimated ops/sec for each path at the given data scale.

    The formulas are intentionally simple, deterministic and integer-friendly;
    NumPy (when installed) only vectorises the log-size percentile estimates,
    never changes the ranking logic, and is fully optional.
    """
    span_seconds = max(span_ms, 1) / 1000.0
    windows = max(span_ms, 1) / max(route.window_ms, 1)

    bucket_ops = 4.0  # refill arithmetic + compare, constant per request
    counter_ops = 6.0  # bucket alignment + weighted estimate + prune
    # Log work grows with the number of retained entries (age-out scan).
    expected_log = min(observed_max_log, route.max_in_window)
    log_ops = 2.0 + 1.5 * expected_log

    bucket_total = bucket_ops * client_count / max(span_seconds, 1e-9)
    counter_total = counter_ops * client_count * windows / max(span_seconds, 1e-9) * 0.25
    log_total = log_ops * client_count / max(span_seconds, 1e-9)
    estimates = {
        PATH_TOKEN_BUCKET: round(bucket_total, 6),
        PATH_WINDOW_COUNTER: round(counter_total, 6),
        PATH_WINDOW_LOG: round(log_total, 6),
    }

    if _numpy_available():
        # Cross-check the vectorised aggregate against the scalar one; the
        # scalar estimate remains authoritative so behaviour cannot drift.
        import numpy as np

        vector = np.array(
            [bucket_ops, counter_ops * windows * 0.25, log_ops], dtype=np.float64
        )
        vectorized = np.round(vector * client_count / max(span_seconds, 1e-9), 6)
        for path, value in zip(LIMIT_PATHS, vectorized):
            if not math.isclose(estimates[path], float(value), rel_tol=0.0, abs_tol=1e-6):
                raise RuntimeError("numpy workload cross-check mismatch")
    return estimates


@dataclass(frozen=True)
class Selection:
    path: str
    reasons: list[str]
    scale: dict[str, Any]
    estimates: dict[str, float]
    tie_broken: bool


def select_path(
    route: Route, client_count: int, span_ms: int, observed_max_log: int
) -> Selection:
    """Pick one path by deterministic rules, never by dict/hash iteration order."""
    estimates = estimate_workloads(route, client_count, span_ms, observed_max_log)
    scale = {
        "client_count": client_count,
        "span_ms": span_ms,
        "window_ms": route.window_ms,
        "max_in_window": route.max_in_window,
        "burst": route.burst,
        "rate_per_sec": route.rate_per_sec,
        "observed_max_log": observed_max_log,
    }
    reasons: list[str] = []

    # Rule 1: eliminate structurally unsuitable candidates at extreme scale.
    eligible = set(LIMIT_PATHS)
    if observed_max_log >= route.max_in_window and client_count >= 100:
        eligible.discard(PATH_WINDOW_LOG)
        reasons.append(
            f"log path eliminated: retained log hits limit {route.max_in_window} "
            f"with {client_count} clients (O(limit) age-out per request)"
        )

    # Rule 2: deterministic ordering of eligible candidates by (workload, fixed
    # path rank) — the rank guarantees identical results on every machine.
    ranked = sorted(
        (estimates[path] for path in eligible),
    )
    best_workload = ranked[0]
    near_tie = [
        path
        for path in LIMIT_PATHS
        if path in eligible and abs(estimates[path] - best_workload) < WORKLOAD_EPSILON
    ]
    tie_broken = len(near_tie) > 1
    if tie_broken:
        chosen = min(near_tie, key=lambda path: _stable_rank(path))
        reasons.append(
            "workload gap below epsilon="
            f"{WORKLOAD_EPSILON} among {sorted(near_tie, key=_stable_rank)}; "
            f"stable rank picks {chosen}"
        )
    else:
        chosen = min(eligible, key=lambda path: (estimates[path], _stable_rank(path)))
        reasons.append(f"lowest estimated workload {estimates[chosen]} ops/sec wins")
    return Selection(chosen, reasons, scale, estimates, tie_broken)


def _stable_rank(path: str) -> int:
    # Fixed, documented precedence for ties: bucket > counter > log.
    ranks = {
        PATH_TOKEN_BUCKET: 0,
        PATH_WINDOW_COUNTER: 1,
        PATH_WINDOW_LOG: 2,
    }
    return ranks[path]
