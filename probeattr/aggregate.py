"""Sliding-window time series aggregation per (service, interface)."""

from collections import defaultdict


def percentile(sorted_values, p):
    """Linear-interpolation percentile on an ascending-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    rank = (n - 1) * p
    low = int(rank)
    high = min(low + 1, n - 1)
    frac = rank - low
    return float(sorted_values[low]) + (sorted_values[high] - sorted_values[low]) * frac


def aggregate(records, window_length):
    """Group records by (service, interface) and compute per-tick window stats.

    Returns {(service, interface): [row, ...]} with rows ordered by tick.
    Never mixes records across services or interfaces.
    """
    by_iface = defaultdict(lambda: defaultdict(list))
    for rec in records:
        by_iface[(rec["service"], rec["interface"])][rec["tick"]].append(rec)

    result = {}
    for key in sorted(by_iface):
        tick_map = by_iface[key]
        rows = []
        for tick in sorted(tick_map):
            window_start = tick - window_length + 1
            window = []
            for t in range(window_start, tick + 1):
                window.extend(tick_map.get(t, ()))
            count = len(window)
            failures = sum(1 for r in window if r["failure_reason"] != "none")
            latencies = sorted(r["latency_ms"] for r in window)
            rows.append({
                "service": key[0],
                "interface": key[1],
                "tick": tick,
                "window_start": window_start,
                "window_end": tick,
                "probe_count": count,
                "failure_count": failures,
                "failure_rate": round(failures / count, 6),
                "p50": round(percentile(latencies, 0.50), 3),
                "p95": round(percentile(latencies, 0.95), 3),
                "p99": round(percentile(latencies, 0.99), 3),
            })
        result[key] = rows
    return result
