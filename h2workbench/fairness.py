"""Priority fairness diagnostics.

Compares each stream's actually granted DATA bytes against the share it
should have received under weighted fairness, and reports starvation
(rounds eligible but not scheduled). Explicit fallback rules:
  * weight <= 0          -> treated as weight 1, annotated weight_fallback
  * weight ineffective   -> a stream eligible while its dependency parent
                            was also eligible has its weight treated as 1,
                            annotated weight_ineffective_fallback
Output rows are sorted by stream id so repeated runs diff cleanly.
"""

STARVATION_ROUND_THRESHOLD = 8

WEIGHT_FALLBACK = "weight_fallback"
WEIGHT_INEFFECTIVE = "weight_ineffective_fallback"


def diagnose_fairness(manifest, plan, actual_sent=None):
    """Build the per-stream fairness table.

    ``actual_sent`` maps stream id to observed DATA bytes; when omitted
    the plan's own accounting is used (plan == observed by construction).
    """
    streams = sorted(manifest["streams"], key=lambda s: s["id"])
    planned_sent = {s["id"]: 0 for s in streams}
    for frame in plan.frames:
        if frame["type"] == "DATA":
            planned_sent[frame["stream_id"]] += frame["bytes"]
    sent = actual_sent if actual_sent is not None else planned_sent

    effective_weights = {}
    annotations = {s["id"]: [] for s in streams}
    for stream in streams:
        sid = stream["id"]
        weight = stream["weight"]
        if weight <= 0:
            weight = 1
            annotations[sid].append(WEIGHT_FALLBACK)
        if sid in plan.parent_overlap:
            weight = 1
            annotations[sid].append(WEIGHT_INEFFECTIVE)
        effective_weights[sid] = weight

    total_weight = sum(effective_weights[s["id"]] for s in streams)
    total_bytes = sum(sent[s["id"]] for s in streams)

    rows = []
    for stream in streams:
        sid = stream["id"]
        expected = (total_bytes * effective_weights[sid] / total_weight
                    if total_weight else 0.0)
        expected = round(expected, 3)
        actual = sent[sid]
        starve = plan.max_starve.get(sid, 0)
        rows.append({
            "stream_id": sid,
            "weight": stream["weight"],
            "effective_weight": effective_weights[sid],
            "expected_bytes": expected,
            "actual_bytes": actual,
            "deviation_bytes": round(actual - expected, 3),
            "max_starved_rounds": starve,
            "starved": starve >= STARVATION_ROUND_THRESHOLD,
            "annotations": annotations[sid],
        })
    rows.sort(key=lambda row: row["stream_id"])  # stable, explicit
    return {
        "total_data_bytes": total_bytes,
        "starvation_round_threshold": STARVATION_ROUND_THRESHOLD,
        "streams": rows,
        "max_abs_deviation_bytes": max(
            (abs(r["deviation_bytes"]) for r in rows), default=0.0),
        "starved_streams": [r["stream_id"] for r in rows if r["starved"]],
    }
