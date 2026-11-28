"""Shared constants, pure numerical helpers and serialisation conventions.

Every value that ends up in a report is produced by one of the deterministic
helpers in this module or by a rule that quotes these constants verbatim.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left

ALGORITHM_VERSION = "1.0.0"
SCHEMA_VERSION = 1
SNAPSHOT_VERSION = 1

GRADES = ("PERFECT", "GOOD", "OFFSET", "MISS")
STATUS_JUDGED = "JUDGED"
STATUS_INVALID = "INVALID"

DEFAULT_POLICY = {
    "window_ratios": {"perfect": 0.08, "good": 0.18, "offset": 0.45, "miss": 0.5},
    "consistency_threshold_ms": 45.0,
    "float_tolerance_ms": 0.001,
    "deviation_thresholds": {
        "mean_abs_deviation_ms": 30.0,
        "accuracy_ratio": 0.2,
    },
}

RULES = {
    "nearest_beat": "minimum absolute time distance; ties choose the earlier beat",
    "windows": "PERFECT<=perfect, GOOD<=good, OFFSET<=offset, otherwise MISS",
    "boundary_inclusion": "all upper boundaries are inclusive",
}

TRACE = {
    "algorithm_version": ALGORITHM_VERSION,
    "correction_formula": "raw_click_ms - median(rtt_samples_ms)/2",
    "nearest_beat_rule": "absolute distance; tie -> lower beat position/index",
    "segment_rule": (
        "corrected click first, then mapped beat; declared timeline order "
        "breaks overlaps"
    ),
    "window_rule": "beat_interval_ms * policy.window_ratios",
}


def group_key(language: str, channel: str) -> str:
    """Canonical key identifying a language/channel RTT profile."""
    return f"{language}|{channel}"


def median(values):
    """Median following :func:`statistics.median` semantics without imports."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise ValueError("median requires at least one value")
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def beat_interval_ms(bpm: float) -> float:
    """Duration of one beat in milliseconds for ``bpm``."""
    return 60000.0 / float(bpm)


def window_bounds(interval_ms: float, ratios: dict) -> dict:
    """Inclusive absolute millisecond boundaries for the four grades."""
    return {
        "perfect": interval_ms * ratios["perfect"],
        "good": interval_ms * ratios["good"],
        "offset": interval_ms * ratios["offset"],
        "miss": interval_ms * ratios["miss"],
    }


def grade_for(abs_deviation_ms: float, boundaries: dict) -> str:
    """Grade from an absolute deviation using inclusive upper boundaries."""
    if abs_deviation_ms <= boundaries["perfect"]:
        return "PERFECT"
    if abs_deviation_ms <= boundaries["good"]:
        return "GOOD"
    if abs_deviation_ms <= boundaries["offset"]:
        return "OFFSET"
    return "MISS"


def nearest_beat(beats_ms, time_ms: float):
    """Return ``(index, beat_ms)`` for the closest beat.

    Ties resolve to the earlier (lower index) beat, matching the quoted
    nearest-beat rule.
    """
    if not beats_ms:
        raise ValueError("song has no beats")
    insert = bisect_left(beats_ms, time_ms)
    if insert == 0:
        return 0, beats_ms[0]
    if insert == len(beats_ms):
        return len(beats_ms) - 1, beats_ms[-1]
    later = beats_ms[insert]
    earlier = beats_ms[insert - 1]
    if (later - time_ms) < (time_ms - earlier):
        return insert, later
    return insert - 1, earlier


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def canonical_bytes(obj) -> bytes:
    """Stable byte representation used for integrity hashes and comparisons."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def clone(obj):
    """Deep copy through JSON so engine state never shares input references."""
    return json.loads(json.dumps(obj, ensure_ascii=False))
