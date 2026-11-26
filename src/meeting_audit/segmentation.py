from __future__ import annotations

from typing import Any

import numpy as np

from .audio import FrameFeatures
from .config import AuditConfig, DEFAULT_CONFIG
from .identity import IdentityAdjudicator, segment_speaker_feature


def _feature_for_range(
    start_ms: int,
    end_ms: int,
    pcm: np.ndarray,
    features: list[FrameFeatures],
    config: AuditConfig,
) -> dict[str, Any]:
    start_frame = max(0, start_ms // config.hop_ms - 1)
    end_frame = max(start_frame, end_ms // config.hop_ms + 1)
    return segment_speaker_feature(
        pcm=pcm,
        frame_features=features,
        start_frame=start_frame,
        end_frame=end_frame,
        config=config,
    )


def merge_segments(
    segments: list[dict[str, Any]],
    merge_ids: list[str],
    pcm: np.ndarray,
    features: list[FrameFeatures],
    config: AuditConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    by_id = {segment["segment_id"]: segment for segment in segments}
    missing = [segment_id for segment_id in merge_ids if segment_id not in by_id]
    if missing:
        raise KeyError(f"unknown segments: {missing}")

    selected = [by_id[segment_id] for segment_id in merge_ids]
    selected.sort(key=lambda item: int(item["start_ms"]))
    positions = sorted(segments.index(segment) for segment in selected)
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise ValueError("merge requires adjacent contiguous segments")

    start_ms = int(selected[0]["start_ms"])
    end_ms = int(selected[-1]["end_ms"])
    first_id = selected[0]["segment_id"]
    last_id = selected[-1]["segment_id"]
    merged_id = f"{first_id}__M__{last_id}"
    parent_lineage: list[str] = []
    for segment in selected:
        parent_lineage.extend(segment.get("lineage", [segment["segment_id"]]))
    merged = {
        "segment_id": merged_id,
        "segment_no": int(selected[0]["segment_no"]),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_sample": start_ms * config.samples_per_ms,
        "end_sample": end_ms * config.samples_per_ms,
        "frame_range": [
            int(selected[0]["frame_range"][0]),
            int(selected[-1]["frame_range"][1]),
        ],
        "speaker_feature": _feature_for_range(
            start_ms, end_ms, pcm, features, config
        ),
        "lineage": sorted(set(parent_lineage + [merged_id])),
        "merged_from": [segment["segment_id"] for segment in selected],
    }

    result: list[dict[str, Any]] = []
    inserted = False
    selected_ids = set(merge_ids)
    for index, segment in enumerate(segments):
        if segment["segment_id"] in selected_ids:
            if not inserted:
                result.append(merged)
                inserted = True
            continue
        result.append(segment)
    return result


def split_segment(
    segments: list[dict[str, Any]],
    segment_id: str,
    split_ms: int,
    pcm: np.ndarray,
    features: list[FrameFeatures],
    config: AuditConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    parent = next(
        (segment for segment in segments if segment["segment_id"] == segment_id), None
    )
    if parent is None:
        raise KeyError(f"unknown segment: {segment_id}")
    start_ms = int(parent["start_ms"])
    end_ms = int(parent["end_ms"])
    if not start_ms < split_ms < end_ms:
        raise ValueError("split_ms must lie strictly inside the segment")

    left_id = f"{segment_id}__L{split_ms}"
    right_id = f"{segment_id}__R{split_ms}"
    lineage = list(parent.get("lineage", [segment_id]))
    left = {
        "segment_id": left_id,
        "segment_no": parent["segment_no"],
        "start_ms": start_ms,
        "end_ms": split_ms,
        "start_sample": start_ms * config.samples_per_ms,
        "end_sample": split_ms * config.samples_per_ms,
        "frame_range": [int(parent["frame_range"][0]), split_ms // config.hop_ms + 1],
        "speaker_feature": _feature_for_range(
            start_ms, split_ms, pcm, features, config
        ),
        "lineage": lineage + [left_id],
        "split_from": segment_id,
    }
    right = {
        "segment_id": right_id,
        "segment_no": parent["segment_no"],
        "start_ms": split_ms,
        "end_ms": end_ms,
        "start_sample": split_ms * config.samples_per_ms,
        "end_sample": end_ms * config.samples_per_ms,
        "frame_range": [split_ms // config.hop_ms - 1, int(parent["frame_range"][1])],
        "speaker_feature": _feature_for_range(
            split_ms, end_ms, pcm, features, config
        ),
        "lineage": lineage + [right_id],
        "split_from": segment_id,
    }

    result = []
    for segment in segments:
        if segment["segment_id"] == segment_id:
            result.extend([left, right])
        else:
            result.append(segment)
    return result


def readjudicate_with_lineage(
    segments: list[dict[str, Any]],
    adjudicator: IdentityAdjudicator,
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, dict[str, Any]]:
    """Recompute only new/revised segments; untouched assignment objects remain."""
    recomputed: list[str] = []
    reused: list[str] = []
    for segment in segments:
        segment_id = str(segment["segment_id"])
        parent_ids = [
            item
            for item in segment.get("lineage", [])
            if item != segment_id and item in adjudicator.assignments
        ]
        if segment_id in adjudicator.assignments:
            reused.append(segment_id)
            continue
        inherited = None
        if parent_ids:
            inherited = adjudicator.inherit_lineage(segment, parent_ids)
        if inherited is None:
            adjudicator.assign(segment, preserve_existing=False)
        recomputed.append(segment_id)
    return {
        "assignments": {
            str(segment["segment_id"]): adjudicator.assignments[str(segment["segment_id"])]
            for segment in segments
        },
        "recomputed": sorted(recomputed),
        "reused": sorted(reused),
    }
