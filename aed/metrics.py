"""Node 4: frame-level and event-level metric aggregation."""

from __future__ import annotations

from .synth import TRUTH_FRAME_MS


def _prf(tp: int, fp: int, fn: int) -> dict:
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 6), "recall": round(rec, 6),
            "f1": round(f1, 6)}


def aggregate_metrics(segments, truth_frames, duration_ms: int,
                      overlap_threshold: float = 0.5) -> dict:
    """Deterministic frame-level and event-level metrics."""
    # rasterize predicted segments onto the 20 ms truth-frame grid
    pred = {}
    for seg in segments:
        f0 = seg["start_ms"] // TRUTH_FRAME_MS
        f1 = -(-seg["end_ms"] // TRUTH_FRAME_MS)  # ceil
        for fi in range(f0, f1):
            pred[fi] = seg["event_type"]

    tp = fp = fn = 0
    for row in truth_frames:
        fi = row["frame_index"]
        gold = row["is_event"] == 1
        hit = fi in pred
        if gold and hit:
            tp += 1
        elif gold:
            fn += 1
        elif hit:
            fp += 1
    frame_metrics = _prf(tp, fp, fn)
    frame_metrics.update({"tp": tp, "fp": fp, "fn": fn})

    # event-level: match when mutual overlap covers >= threshold of the
    # shorter of the predicted/gold spans (containment-style matching)
    gold_events = _truth_events(truth_frames)
    matched_gold = set()
    ev_tp = 0
    for seg in segments:
        best_cov, best_gi = 0.0, -1
        for gi, (g0, g1) in enumerate(gold_events):
            inter = max(0, min(seg["end_ms"], g1) - max(seg["start_ms"], g0))
            shorter = max(1, min(seg["end_ms"] - seg["start_ms"], g1 - g0))
            cov = inter / shorter
            if cov > best_cov:
                best_cov, best_gi = cov, gi
        if best_cov >= overlap_threshold and best_gi not in matched_gold:
            matched_gold.add(best_gi)
            ev_tp += 1
    ev_fp = len(segments) - ev_tp
    ev_fn = len(gold_events) - ev_tp
    event_metrics = _prf(ev_tp, ev_fp, ev_fn)
    event_metrics.update({"tp": ev_tp, "fp": ev_fp, "fn": ev_fn})

    return {
        "duration_ms": duration_ms,
        "n_frames": len(truth_frames),
        "n_pred_events": len(segments),
        "n_gold_events": len(gold_events),
        "frame_level": frame_metrics,
        "event_level": event_metrics,
    }


def _truth_events(truth_frames) -> list:
    events = []
    cur_type, cur_start, prev_end = None, None, None
    for row in truth_frames:
        if row["is_event"] == 1:
            s0 = row["frame_index"] * TRUTH_FRAME_MS
            s1 = s0 + TRUTH_FRAME_MS
            if cur_type == row["event_type"] and s0 <= (prev_end or 0):
                prev_end = s1
            else:
                if cur_type is not None:
                    events.append((cur_start, prev_end))
                cur_type, cur_start, prev_end = row["event_type"], s0, s1
    if cur_type is not None:
        events.append((cur_start, prev_end))
    return events
