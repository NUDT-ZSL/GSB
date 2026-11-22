"""Single causal sliding-window definition used by both execution paths."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .config import EngineConfig
from .errors import E_WINDOW_RECOUNT, EngineError
from .tiering import dominant_tier, tier_counts
from .util import rounded


WINDOW_SEMANTICS = {
    "name": "causal_trailing",
    "boundary_policy": "truncate_left_no_pad",
    "insufficient_policy": "use_available_frames_no_fill",
    "padding_frames": 0,
    "window_contains_future_frames": False,
}


def window_bounds(frame_index: int, frame_count: int, length_frames: int) -> tuple[int, int]:
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if not 0 <= frame_index < frame_count:
        raise ValueError("frame_index outside frame range")
    start = max(0, frame_index - length_frames + 1)
    return start, frame_index


def compute_window(
    frame_index: int,
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> dict[str, Any]:
    start, end = window_bounds(frame_index, len(frames), config.window_length)
    members = frames[start : end + 1]
    guard = config.subnormal_guard
    valence_mean = rounded(
        math.fsum(float(frame["valence"]) for frame in members) / len(members),
        config.score_decimals,
        guard,
    )
    arousal_mean = rounded(
        math.fsum(float(frame["arousal"]) for frame in members) / len(members),
        config.score_decimals,
        guard,
    )
    counts = tier_counts([str(frame["instant_tier"]) for frame in members], config)
    tier = dominant_tier(counts, config)
    return {
        "window_start_frame": start,
        "window_end_frame": end,
        "window_size": len(members),
        "insufficient_window": len(members) < config.window_length,
        "padding_frames": 0,
        "valence_mean": valence_mean,
        "arousal_mean": arousal_mean,
        "tier_counts": counts,
        "tier": tier,
    }


def attach_windows(
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        item = dict(frame)
        window = compute_window(frame_index, frames, config)
        item["window"] = window
        item["tier"] = window["tier"]
        output.append(item)
    return output


def recount_window(
    frame_index: int,
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> dict[str, Any]:
    if not 0 <= frame_index < len(frames):
        raise EngineError(
            E_WINDOW_RECOUNT,
            "cannot recount a window for a frame outside the frame set",
            frame_index=frame_index,
            frame_count=len(frames),
        )
    expected = frames[frame_index]["window"]
    actual = compute_window(frame_index, frames, config)
    differences: dict[str, Any] = {}
    for field in ("window_start_frame", "window_end_frame", "window_size", "padding_frames"):
        if int(expected[field]) != int(actual[field]):
            differences[field] = {"snapshot": expected[field], "recount": actual[field]}
    if bool(expected["insufficient_window"]) != bool(actual["insufficient_window"]):
        differences["insufficient_window"] = {
            "snapshot": expected["insufficient_window"],
            "recount": actual["insufficient_window"],
        }
    if str(expected["tier"]) != str(actual["tier"]):
        differences["tier"] = {"snapshot": expected["tier"], "recount": actual["tier"]}
    if dict(expected["tier_counts"]) != dict(actual["tier_counts"]):
        differences["tier_counts"] = {
            "snapshot": expected["tier_counts"],
            "recount": actual["tier_counts"],
        }
    for field in ("valence_mean", "arousal_mean"):
        if abs(float(expected[field]) - float(actual[field])) > config.comparison_abs_tol:
            differences[field] = {
                "snapshot": expected[field],
                "recount": actual[field],
                "abs_delta": abs(float(expected[field]) - float(actual[field])),
            }
    if differences:
        raise EngineError(
            E_WINDOW_RECOUNT,
            "window recount disagrees with the materialized snapshot",
            frame_index=frame_index,
            differences=differences,
        )
    return actual
