from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def deterministic_unit(seed: int, *parts: Any) -> float:
    payload = json.dumps(
        [seed, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def _matches_selector(selector: Any, gradient_index: int, window_index: int) -> bool:
    if isinstance(selector, int) and not isinstance(selector, bool):
        return selector == window_index
    if isinstance(selector, str):
        try:
            return int(selector) == window_index
        except ValueError:
            return False
    if isinstance(selector, dict):
        window_value = selector.get("window_index", selector.get("window"))
        gradient_value = selector.get(
            "gradient_index",
            selector.get("gradient"),
        )
        if window_value is not None:
            windows = window_value if isinstance(window_value, list) else [window_value]
            if window_index not in [int(value) for value in windows]:
                return False
        if gradient_value is not None:
            gradients = (
                gradient_value
                if isinstance(gradient_value, list)
                else [gradient_value]
            )
            if gradient_index not in [int(value) for value in gradients]:
                return False
        return True
    return False


def _matches_any(values: Any, gradient_index: int, window_index: int) -> bool:
    if not isinstance(values, list):
        return False
    return any(
        _matches_selector(selector, gradient_index, window_index)
        for selector in values
    )


def _weighted_status(
    distribution: dict[str, float],
    unit: float,
) -> int:
    cursor = 0.0
    for status, weight in sorted(distribution.items(), key=lambda item: int(item[0])):
        cursor += float(weight)
        if unit < cursor:
            return int(status)
    return int(sorted(distribution, key=int)[-1])


def simulate_response(
    *,
    profile: dict[str, Any],
    seed: int,
    tenant: str,
    gradient_index: int,
    window_index: int,
    request_seq: int,
    concurrency: int,
) -> dict[str, Any]:
    """Return a deterministic simulated target response.

    The result is keyed by request sequence rather than call order, so process
    restarts, retries and parallel execution all observe the same response.
    """

    status_unit = deterministic_unit(
        seed,
        "status",
        tenant,
        gradient_index,
        window_index,
        request_seq,
    )
    latency_unit = deterministic_unit(
        seed,
        "latency",
        tenant,
        gradient_index,
        window_index,
        request_seq,
    )
    outcome = "ok"
    malformed = False
    forced_status: int | None = None

    if _matches_any(profile.get("refused_windows", []), gradient_index, window_index):
        outcome = "connection_refused"
    elif _matches_any(profile.get("fault_windows", []), gradient_index, window_index):
        outcome = "interrupted"
        forced_status = 503
    elif _matches_any(
        profile.get("malformed_windows", []),
        gradient_index,
        window_index,
    ):
        outcome = "malformed"
        malformed = True

    distribution = profile.get("status_distribution", {"200": 1.0})
    status = forced_status if forced_status is not None else _weighted_status(
        distribution,
        status_unit,
    )
    latency_profile = profile.get("latency", {})
    base_ms = float(latency_profile.get("base_ms", 10.0))
    jitter_ms = float(latency_profile.get("jitter_ms", 0.0))
    per_concurrency_ms = float(latency_profile.get("per_concurrency_ms", 0.0))
    overload_penalty_ms = float(latency_profile.get("overload_penalty_ms", 0.0))
    knee = latency_profile.get("knee_concurrency")
    latency = base_ms + jitter_ms * latency_unit + per_concurrency_ms * concurrency
    if knee is not None and concurrency > int(knee):
        latency += overload_penalty_ms
    if _matches_any(profile.get("slow_windows", []), gradient_index, window_index):
        latency += float(latency_profile.get("slow_extra_ms", 500.0))
    latency = round(max(0.0, latency), 6)

    return {
        "status": status,
        "simulated_latency_ms": latency,
        "outcome": outcome,
        "malformed": malformed,
        "tenant": tenant,
        "gradient_index": gradient_index,
        "window_index": window_index,
        "request_seq": request_seq,
        "concurrency": concurrency,
        "ok": outcome == "ok" and not malformed,
    }

