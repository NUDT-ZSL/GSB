"""Speaker-contiguous segment construction and independent recount."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .config import EngineConfig
from .errors import E_SEGMENT_BOUNDARY, EngineError
from .tiering import dominant_tier, tier_counts
from .util import rounded


def _weighted_mean(
    values: Sequence[float],
    weights: Sequence[float],
    config: EngineConfig,
) -> float:
    total_weight = math.fsum(float(weight) for weight in weights)
    if total_weight <= config.subnormal_guard:
        return 0.0
    numerator = math.fsum(
        float(value) * float(weight)
        for value, weight in zip(values, weights)
    )
    return rounded(numerator / total_weight, config.score_decimals, config.subnormal_guard)


def _summarize_segment(
    segment_id: str,
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> dict[str, Any]:
    start_frame = int(frames[0]["frame_index"])
    end_frame = int(frames[-1]["frame_index"])
    if len(frames) != end_frame - start_frame + 1:
        raise EngineError(
            E_SEGMENT_BOUNDARY,
            "segment frames are not contiguous",
            segment_id=segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            frame_count=len(frames),
        )
    weights = [float(frame["frame_weight"]) for frame in frames]
    counts = tier_counts([str(frame["tier"]) for frame in frames], config)
    total_weight = rounded(
        math.fsum(weights),
        config.score_decimals,
        config.subnormal_guard,
    )
    return {
        "segment_id": segment_id,
        "speaker_id": str(frames[0]["speaker_id"]),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": len(frames),
        "start_sample": int(frames[0]["start_sample"]),
        "end_sample": int(frames[-1]["end_sample"]),
        "frame_weight_definition": "energy_rms_floor_1e-9",
        "total_frame_weight": total_weight,
        "valence": _weighted_mean([float(frame["valence"]) for frame in frames], weights, config),
        "arousal": _weighted_mean([float(frame["arousal"]) for frame in frames], weights, config),
        "tier_counts": counts,
        "tier": dominant_tier(counts, config),
        "speech_frame_count": sum(1 for frame in frames if bool(frame["is_speech"])),
    }


def build_segments(
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> list[dict[str, Any]]:
    if not frames:
        return []
    segments: list[dict[str, Any]] = []
    start = 0
    for index in range(1, len(frames) + 1):
        boundary = index == len(frames) or str(frames[index]["speaker_id"]) != str(frames[start]["speaker_id"])
        if boundary:
            segment_id = f"seg_{len(segments):04d}"
            segments.append(_summarize_segment(segment_id, frames[start:index], config))
            start = index
    validate_segment_boundaries(segments, [str(frame["speaker_id"]) for frame in frames])
    return segments


def validate_segment_boundaries(
    segments: Sequence[Mapping[str, Any]],
    speakers: Sequence[str],
) -> None:
    frame_count = len(speakers)
    if frame_count == 0:
        if segments:
            raise EngineError(
                E_SEGMENT_BOUNDARY,
                "segments exist for an empty frame set",
                first_deviating_frame=0,
            )
        return
    expected_starts = [0]
    expected_starts.extend(
        index
        for index in range(1, frame_count)
        if str(speakers[index]) != str(speakers[index - 1])
    )
    actual_starts = [int(segment["start_frame"]) for segment in segments]
    if actual_starts != expected_starts:
        first = 0
        for expected, actual in zip(expected_starts, actual_starts):
            if expected != actual:
                first = min(expected, actual)
                break
        else:
            first = min(len(expected_starts), len(actual_starts))
        raise EngineError(
            E_SEGMENT_BOUNDARY,
            "segment starts are not aligned with speaker label changes",
            first_deviating_frame=first,
            expected_starts=expected_starts,
            actual_starts=actual_starts,
        )
    for index, segment in enumerate(segments):
        expected_end = (
            expected_starts[index + 1] - 1
            if index + 1 < len(expected_starts)
            else frame_count - 1
        )
        if int(segment["end_frame"]) != expected_end:
            raise EngineError(
                E_SEGMENT_BOUNDARY,
                "segment end is not aligned with the next speaker switch",
                first_deviating_frame=min(expected_end, int(segment["end_frame"]) + 1),
                segment_id=str(segment.get("segment_id", f"seg_{index:04d}")),
                expected_end=expected_end,
                actual_end=int(segment["end_frame"]),
            )
        if str(segment["speaker_id"]) != str(speakers[expected_starts[index]]):
            raise EngineError(
                E_SEGMENT_BOUNDARY,
                "segment speaker does not match the frame label",
                first_deviating_frame=expected_starts[index],
                segment_id=str(segment.get("segment_id", f"seg_{index:04d}")),
                expected_speaker=str(speakers[expected_starts[index]]),
                actual_speaker=str(segment["speaker_id"]),
            )


def recount_segment(
    segment: Mapping[str, Any],
    frames: Sequence[Mapping[str, Any]],
    config: EngineConfig,
) -> dict[str, Any]:
    start = int(segment["start_frame"])
    end = int(segment["end_frame"])
    if start < 0 or end >= len(frames) or start > end:
        raise EngineError(
            E_SEGMENT_BOUNDARY,
            "segment range is outside the frame set",
            first_deviating_frame=max(0, min(start, end)),
            start_frame=start,
            end_frame=end,
            frame_count=len(frames),
        )
    actual = _summarize_segment(str(segment["segment_id"]), frames[start : end + 1], config)
    differences: dict[str, Any] = {}
    for field in (
        "speaker_id",
        "start_frame",
        "end_frame",
        "frame_count",
        "start_sample",
        "end_sample",
        "total_frame_weight",
        "tier",
        "speech_frame_count",
    ):
        if segment[field] != actual[field]:
            differences[field] = {"snapshot": segment[field], "recount": actual[field]}
    if dict(segment["tier_counts"]) != dict(actual["tier_counts"]):
        differences["tier_counts"] = {
            "snapshot": segment["tier_counts"],
            "recount": actual["tier_counts"],
        }
    for field in ("valence", "arousal"):
        if abs(float(segment[field]) - float(actual[field])) > config.comparison_abs_tol:
            differences[field] = {
                "snapshot": segment[field],
                "recount": actual[field],
                "abs_delta": abs(float(segment[field]) - float(actual[field])),
            }
    if differences:
        raise EngineError(
            E_SEGMENT_BOUNDARY,
            "segment summary recount disagrees with the snapshot",
            first_deviating_frame=start,
            segment_id=str(segment["segment_id"]),
            differences=differences,
        )
    return actual
