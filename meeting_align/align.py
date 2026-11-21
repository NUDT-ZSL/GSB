"""Map transcript segments onto the global axis and adjudicate conflicts."""

from __future__ import annotations

from .errors import OutOfRangeError, OverlapError
from .util import round_ms


def map_segment(segment, calibration):
    """Map local ms to global ms via the batch calibration."""
    scale, offset = calibration["scale"], calibration["offset_ms"]
    return (
        round_ms(scale * segment["start_ms"] + offset),
        round_ms(scale * segment["end_ms"] + offset),
    )


def _conflicts(start_ms, end_ms, seams_global_ms, gaps_global_ms):
    crossed = [s for s in seams_global_ms if start_ms < s < end_ms]
    hit_gaps = [g for g in gaps_global_ms if start_ms < g[1] and end_ms > g[0]]
    return crossed, hit_gaps


def resolve_segment(segment, calibration, seams_global_ms, gaps_global_ms, config):
    """Map one segment and resolve seam/gap conflicts per the config strategy.

    Returns a mapped segment dict with handling category
    "none" | "truncated" | "shifted".
    """
    scale = calibration["scale"]
    orig_start, orig_end = map_segment(segment, calibration)
    g_start, g_end = orig_start, orig_end
    crossed, hit_gaps = _conflicts(orig_start, orig_end, seams_global_ms, gaps_global_ms)
    handling = "none"
    shift_ms = 0.0

    if crossed or hit_gaps:
        if config.conflict_strategy == "truncate":
            boundaries = sorted(
                crossed
                + [g[0] for g in hit_gaps if g[0] > orig_start]
                + [g[1] for g in hit_gaps if g[1] < orig_end]
            )
            end_cuts = [b for b in boundaries if b > orig_start]
            new_end = min(end_cuts) if end_cuts else orig_end
            if new_end - orig_start >= config.min_segment_ms:
                g_end = round_ms(new_end)
            else:
                head_cuts = [b for b in boundaries if b < orig_end]
                new_start = max(head_cuts) if head_cuts else orig_start
                if orig_end - new_start >= config.min_segment_ms:
                    g_start = round_ms(new_start)
            handling = "truncated"
        else:  # shift the whole segment past the conflict
            shift_to = max([g[1] for g in hit_gaps] + crossed)
            shift_ms = round_ms(shift_to - orig_start)
            g_start = round_ms(orig_start + shift_ms)
            g_end = round_ms(orig_end + shift_ms)
            handling = "shifted"

    kept_words, dropped = [], []
    for w in segment["words"]:
        word_global = round_ms(orig_start + scale * w["offset_ms"] + shift_ms)
        if g_start <= word_global < g_end:
            kept_words.append({**w, "global_ms": word_global})
        else:
            dropped.append(w["text"])

    return {
        "id": segment["id"],
        "batch_id": segment["batch_id"],
        "speaker": segment["speaker"],
        "start_ms": g_start,
        "end_ms": g_end,
        "local_duration_ms": round_ms(segment["end_ms"] - segment["start_ms"]),
        "handling": handling,
        "shift_ms": round_ms(shift_ms),
        "dropped_words": dropped,
        "words": kept_words,
    }


def validate_global_segments(mapped, total_duration_ms):
    """Enforce ascending order, non-overlap and range; raise business errors."""
    ordered = sorted(mapped, key=lambda s: (s["start_ms"], s["id"]))
    prev_end = None
    for seg in ordered:
        if prev_end is not None and seg["start_ms"] < prev_end:
            raise OverlapError(
                f"segment {seg['id']} overlaps a previous segment",
                segment_id=seg["id"],
                start_ms=seg["start_ms"],
                previous_end_ms=prev_end,
            )
        if (
            seg["start_ms"] < 0
            or seg["end_ms"] > total_duration_ms
            or seg["end_ms"] <= seg["start_ms"]
        ):
            raise OutOfRangeError(
                f"segment {seg['id']} outside [0, {total_duration_ms}] ms",
                segment_id=seg["id"],
                start_ms=seg["start_ms"],
                end_ms=seg["end_ms"],
                total_duration_ms=total_duration_ms,
            )
        prev_end = seg["end_ms"]
    return ordered
