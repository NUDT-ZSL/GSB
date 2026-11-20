"""Continuous speaker segmentation and coverage validation."""

from __future__ import annotations

import math
from typing import Any

from .canonical import content_hash, quantize
from .errors import fail


def build_segments(
    dialog_id: str,
    labels: list[str],
    threshold_ms: float,
    hop_ms: float,
    frame_count: int,
) -> dict[str, Any]:
    if len(labels) != frame_count:
        fail(
            "E_SEGMENT_GAP",
            "speaker labels do not cover every feature frame",
            dialog_id=dialog_id,
            label_count=len(labels),
            frame_count=frame_count,
        )
    runs: list[dict[str, Any]] = []
    if frame_count:
        start = 0
        current = labels[0]
        for index in range(1, frame_count):
            if labels[index] != current:
                runs.append(
                    {
                        "start_frame": start,
                        "end_frame": index,
                        "speaker_id": current,
                    }
                )
                start = index
                current = labels[index]
        runs.append(
            {
                "start_frame": start,
                "end_frame": frame_count,
                "speaker_id": current,
            }
        )

    threshold_frames = max(1, int(math.ceil(threshold_ms / hop_ms - 1e-12)))
    while len(runs) > 1:
        short_indices = [
            index
            for index, run in enumerate(runs)
            if run["end_frame"] - run["start_frame"] < threshold_frames
        ]
        if not short_indices:
            break
        index = min(
            short_indices,
            key=lambda item: (
                runs[item]["end_frame"] - runs[item]["start_frame"],
                runs[item]["start_frame"],
            ),
        )
        if index == 0:
            neighbor = 1
        elif index == len(runs) - 1:
            neighbor = index - 1
        else:
            previous_len = runs[index - 1]["end_frame"] - runs[index - 1]["start_frame"]
            next_len = runs[index + 1]["end_frame"] - runs[index + 1]["start_frame"]
            neighbor = index - 1 if previous_len >= next_len else index + 1
        left = min(index, neighbor)
        right = max(index, neighbor)
        merged = {
            "start_frame": runs[left]["start_frame"],
            "end_frame": runs[right]["end_frame"],
            "speaker_id": (
                runs[neighbor]["speaker_id"]
                if runs[left]["speaker_id"] == runs[right]["speaker_id"]
                else runs[neighbor]["speaker_id"]
            ),
        }
        runs[left : right + 1] = [merged]
        _coalesce_runs(runs)

    segments: list[dict[str, Any]] = []
    for index, run in enumerate(runs):
        start_frame = int(run["start_frame"])
        end_frame = int(run["end_frame"])
        duration_ms = quantize((end_frame - start_frame) * hop_ms)
        segments.append(
            {
                "segment_index": index,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frame_count": end_frame - start_frame,
                "start_ms": quantize(start_frame * hop_ms),
                "end_ms": quantize(end_frame * hop_ms),
                "duration_ms": duration_ms,
                "speaker_id": run["speaker_id"],
            }
        )
    validate_segments(dialog_id, segments, frame_count)
    return {
        "dialog_id": dialog_id,
        "hop_ms": hop_ms,
        "segment_threshold_ms": threshold_ms,
        "frame_count": frame_count,
        "segments": segments,
        "segments_hash": content_hash(segments),
    }


def validate_segments(
    dialog_id: str,
    segments: list[dict[str, Any]],
    frame_count: int,
) -> None:
    cursor = 0
    for segment in segments:
        if int(segment["start_frame"]) != cursor:
            fail(
                "E_SEGMENT_GAP",
                "segments must be contiguous and cover every frame",
                dialog_id=dialog_id,
                expected_start_frame=cursor,
                actual_start_frame=segment["start_frame"],
                segment_index=segment.get("segment_index"),
            )
        cursor = int(segment["end_frame"])
    if cursor != frame_count:
        fail(
            "E_SEGMENT_GAP",
            "segments do not cover the final frame",
            dialog_id=dialog_id,
            expected_end_frame=frame_count,
            actual_end_frame=cursor,
        )


def _coalesce_runs(runs: list[dict[str, Any]]) -> None:
    index = 1
    while index < len(runs):
        if runs[index - 1]["speaker_id"] == runs[index]["speaker_id"]:
            runs[index - 1]["end_frame"] = runs[index]["end_frame"]
            del runs[index]
        else:
            index += 1
