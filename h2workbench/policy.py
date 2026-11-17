"""Scale-adaptive strategy selection.

Deterministic rule: simulate every candidate strategy on the declared
input, estimate each one's stall cost (total predicted window-exhaustion
stall milliseconds), and pick the cheapest. When the cost difference
between the best candidates is within the explicit tie threshold, the
fixed priority order STRATEGY_PRIORITY breaks the tie. No dict or hash
iteration order is ever consulted.
"""

from .planner import simulate
from .scheduler import STRATEGIES

# Stable tie-break priority: most fairness-aware first.
STRATEGY_PRIORITY = ("weighted_rr", "window_first", "arrival")

STRATEGY_LABELS = {
    "weighted_rr": "weighted round-robin",
    "window_first": "largest remaining window first",
    "arrival": "arrival (declaration) order",
}


def scale_parameters(manifest):
    streams = manifest["streams"]
    weights = [s["weight"] for s in streams]
    total_body = sum(s["body_bytes"] for s in streams)
    conn = manifest["connection"]
    total_window = (conn["client_send"]["stream_window"] * len(streams)
                    + conn["client_send"]["connection_window"])
    return {
        "stream_count": len(streams),
        "max_concurrent_streams": conn["max_concurrent_streams"],
        "weight_min": min(weights),
        "weight_max": max(weights),
        "weight_spread": max(weights) / min(weights),
        "total_body_bytes": total_body,
        "stream_window": conn["client_send"]["stream_window"],
        "connection_window": conn["client_send"]["connection_window"],
        "window_to_body_ratio": (total_window / total_body
                                 if total_body else float("inf")),
    }


def estimate_costs(manifest):
    """Simulate every candidate; return plans keyed by strategy name."""
    plans = {}
    for name in STRATEGIES:  # fixed tuple order, not dict order
        plans[name] = simulate(manifest, name)
    return plans


def select_strategy(manifest, forced=None):
    """Choose the default strategy; returns (decision, plans).

    ``decision`` carries the selected name, a human-readable reason and
    the key scale parameters; ``plans`` maps strategy name to its
    simulated plan so the caller can reuse the selected plan directly.
    """
    plans = estimate_costs(manifest)
    params = scale_parameters(manifest)
    threshold = manifest["policy"]["tie_threshold_ms"]
    costs = {name: sum(s["end_ms"] - s["start_ms"]
                       for s in plans[name].stalls)
             for name in STRATEGIES}

    if forced is not None:
        if forced not in STRATEGIES:
            raise ValueError(f"unknown strategy: {forced}")
        decision = {
            "selected": forced,
            "auto_selected": _cheapest(costs, threshold),
            "reason": (f"strategy forced by caller; scale parameters: "
                       f"streams={params['stream_count']}, "
                       f"weight_spread={params['weight_spread']:.2f}, "
                       f"window_to_body="
                       f"{params['window_to_body_ratio']:.3f}"),
            "tie_threshold_ms": threshold,
            "estimated_stall_costs_ms": costs,
            "scale_parameters": params,
        }
        return decision, plans

    selected = _cheapest(costs, threshold)
    ordered = sorted(costs.items(), key=lambda kv: (kv[1], kv[0]))
    best_cost = ordered[0][1]
    tied = [name for name, cost in ordered
            if cost - best_cost <= threshold]
    reason = (
        f"streams={params['stream_count']} "
        f"max_concurrent={params['max_concurrent_streams']} "
        f"weight_spread={params['weight_spread']:.2f} "
        f"window_to_body={params['window_to_body_ratio']:.3f}; "
        f"estimated stall cost ms: "
        + ", ".join(f"{name}={costs[name]}" for name in STRATEGIES)
        + f"; tie_threshold={threshold}ms"
    )
    if len(tied) > 1:
        reason += ("; candidates within tie threshold "
                   f"{tied}, stable priority order "
                   f"{list(STRATEGY_PRIORITY)} selected '{selected}'")
    else:
        reason += f"; lowest estimated stall cost selected '{selected}'"
    decision = {
        "selected": selected,
        "auto_selected": selected,
        "reason": reason,
        "tie_threshold_ms": threshold,
        "estimated_stall_costs_ms": costs,
        "scale_parameters": params,
    }
    return decision, plans


def _cheapest(costs, threshold):
    best_cost = min(costs[name] for name in STRATEGIES)
    for name in STRATEGY_PRIORITY:
        if costs[name] - best_cost <= threshold:
            return name
    return STRATEGY_PRIORITY[-1]
