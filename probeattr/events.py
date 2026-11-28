"""Availability-degradation event detection on aggregated series."""

from .aggregate import percentile

_PERCENTILE_VALUE = {"p50": 0.50, "p95": 0.95, "p99": 0.99}


def _runs(flags):
    """Yield (start_idx, end_idx) inclusive runs of True values."""
    start = None
    for idx, flag in enumerate(flags):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            yield start, idx - 1
            start = None
    if start is not None:
        yield start, len(flags) - 1


def detect_events(aggregates, records, strategy):
    """Return (events, sporadic_failures).

    events: list of dicts with service/interface/type/start/end and stats.
    sporadic_failures: isolated failures below the failure-rate threshold that
    are not covered by any event.
    """
    fail_threshold = strategy["failure_rate_threshold"]
    consec = strategy["consecutive_failure_threshold"]
    lat_consec = strategy["latency_consecutive_threshold"]
    lat_threshold = strategy["latency_threshold_ms"]
    lat_p = _PERCENTILE_VALUE[strategy["latency_percentile"]]
    window = strategy["window_length"]

    by_iface = {}
    for rec in records:
        by_iface.setdefault((rec["service"], rec["interface"]), []).append(rec)
    by_iface_tick = {}
    for key, recs in by_iface.items():
        tick_map = {}
        for rec in recs:
            tick_map.setdefault(rec["tick"], []).append(rec)
        by_iface_tick[key] = tick_map

    events = []
    sporadic = []
    for key in sorted(aggregates):
        rows = aggregates[key]
        service, interface = key
        fail_flags = [r["failure_rate"] >= fail_threshold for r in rows]
        tick_map = by_iface_tick.get(key, {})
        lat_flags = []
        for row in rows:
            ok_latencies = []
            for t in range(row["tick"] - window + 1, row["tick"] + 1):
                for rec in tick_map.get(t, ()):
                    if rec["failure_reason"] == "none":
                        ok_latencies.append(rec["latency_ms"])
            ok_latencies.sort()
            p_val = percentile(ok_latencies, lat_p)
            lat_flags.append(p_val is not None and p_val >= lat_threshold)

        event_spans = []
        for start_idx, end_idx in _runs(fail_flags):
            if end_idx - start_idx + 1 < consec:
                continue
            span_rows = rows[start_idx:end_idx + 1]
            start_tick = span_rows[0]["tick"]
            end_tick = span_rows[-1]["tick"]
            modes = {}
            for rec in by_iface.get(key, ()):
                if start_tick <= rec["tick"] <= end_tick and \
                        rec["failure_reason"] != "none":
                    modes[rec["failure_reason"]] = modes.get(rec["failure_reason"], 0) + 1
            dominant = max(sorted(modes), key=lambda m: modes[m]) if modes else "unknown"
            events.append({
                "service": service,
                "interface": interface,
                "type": "sustained_failure",
                "start_tick": start_tick,
                "end_tick": end_tick,
                "failure_modes": dict(sorted(modes.items())),
                "dominant_failure_mode": dominant,
                "max_failure_rate": max(r["failure_rate"] for r in span_rows),
                "max_p95_ms": max(r["p95"] for r in span_rows),
            })
            event_spans.append((start_tick, end_tick))

        for start_idx, end_idx in _runs(lat_flags):
            if end_idx - start_idx + 1 < lat_consec:
                continue
            span_rows = rows[start_idx:end_idx + 1]
            start_tick = span_rows[0]["tick"]
            end_tick = span_rows[-1]["tick"]
            events.append({
                "service": service,
                "interface": interface,
                "type": "latency_degradation",
                "start_tick": start_tick,
                "end_tick": end_tick,
                "failure_modes": {},
                "dominant_failure_mode": "latency",
                "max_failure_rate": max(r["failure_rate"] for r in span_rows),
                "max_p95_ms": max(r["p95"] for r in span_rows),
            })
            event_spans.append((start_tick, end_tick))

        for row in rows:
            tick_failures = sum(1 for rec in tick_map.get(row["tick"], ())
                                if rec["failure_reason"] != "none")
            if tick_failures == 0:
                continue
            if row["failure_rate"] >= fail_threshold:
                continue
            if any(s <= row["tick"] <= e for s, e in event_spans):
                continue
            sporadic.append({
                "service": service,
                "interface": interface,
                "tick": row["tick"],
                "failure_count": tick_failures,
                "failure_rate": row["failure_rate"],
            })

    events.sort(key=lambda e: (e["start_tick"], e["service"], e["interface"], e["type"]))
    for idx, event in enumerate(events, start=1):
        event["event_id"] = f"EVT-{idx:04d}"
    sporadic.sort(key=lambda s: (s["tick"], s["service"], s["interface"]))
    return events, sporadic
