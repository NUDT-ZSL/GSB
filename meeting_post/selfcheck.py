"""Cross-module consistency checks for segmentation, masks and identities."""

from __future__ import annotations

from typing import Any

from .errors import MeetingError
from .timebase import verify_span_timebase


def run_self_check(result: Any, *, raise_on_error: bool = False) -> dict[str, Any]:
    config = result.config
    issues: list[dict[str, Any]] = []
    candidate_frames: set[int] = set()
    for candidate in result.separation.candidates:
        start = int(candidate["start_frame"])
        end = int(candidate["end_frame"])
        candidate_frames.update(range(start, end))
        if end <= start:
            issues.append(
                {
                    "check": "candidate_nonzero_span",
                    "candidate_id": candidate["candidate_id"],
                }
            )
    mask_frames = {
        frame_index
        for frame_index in range(result.separation.masks.shape[0])
        if bool((result.separation.masks[frame_index] > 0.0).any())
    }
    if candidate_frames != mask_frames:
        issues.append(
            {
                "check": "segmentation_mask_coverage",
                "missing_from_masks": sorted(candidate_frames - mask_frames)[:20],
                "missing_from_candidates": sorted(mask_frames - candidate_frames)[:20],
            }
        )
    segment_frames: set[int] = set()
    for segment in result.separation.segments:
        segment_frames.update(segment.frame_indices)
    if segment_frames != mask_frames:
        issues.append(
            {
                "check": "separated_segment_mask_coverage",
                "missing_from_segments": sorted(mask_frames - segment_frames)[:20],
                "missing_from_masks": sorted(segment_frames - mask_frames)[:20],
            }
        )

    segment_ids = [segment.segment_id for segment in result.separation.segments]
    identity_ids = [identity.segment_id for identity in result.identities]
    if sorted(segment_ids) != sorted(identity_ids):
        issues.append(
            {
                "check": "identity_segment_one_to_one",
                "segment_ids": segment_ids,
                "identity_ids": identity_ids,
            }
        )
    if len(set(identity_ids)) != len(identity_ids):
        issues.append({"check": "identity_unique_segments"})

    timebase_rows: list[dict[str, Any]] = []
    within_duration = True
    for segment in result.separation.segments:
        try:
            row = verify_span_timebase(
                segment,
                sample_rate=config.sample_rate,
                frame_samples=config.frame_samples,
                hop_samples=config.hop_samples,
            )
            timebase_rows.append(row)
        except MeetingError as exc:
            issues.append({"check": "timebase_roundtrip", "error": exc.to_dict()})
        if segment.end_sample > config.total_samples or segment.start_sample < 0:
            within_duration = False
            issues.append(
                {
                    "check": "segment_within_audio",
                    "segment_id": segment.segment_id,
                    "start_sample": segment.start_sample,
                    "end_sample": segment.end_sample,
                    "total_samples": config.total_samples,
                }
            )
    if not within_duration:
        issues.append({"check": "all_segments_within_audio"})

    ordered_segments = sorted(
        result.separation.segments,
        key=lambda item: (item.start_sample, item.end_sample, item.speaker_id),
    )
    for previous, current in zip(ordered_segments, ordered_segments[1:]):
        if current.start_sample < previous.end_sample:
            issues.append(
                {
                    "check": "no_overlap",
                    "previous_segment_id": previous.segment_id,
                    "segment_id": current.segment_id,
                }
            )

    declared = set(range(1, config.max_speakers + 1))
    mismatched = [
        identity.segment_id
        for identity in result.identities
        if not identity.matches_truth
    ]
    unknown_labels = [
        identity.segment_id
        for identity in result.identities
        if identity.speaker_id not in declared
    ]
    if mismatched:
        issues.append(
            {
                "check": "identity_truth_match",
                "segment_ids": mismatched,
            }
        )
    if unknown_labels:
        issues.append(
            {
                "check": "identity_declared_speakers",
                "segment_ids": unknown_labels,
            }
        )

    report = {
        "ok": not issues,
        "checks": {
            "segmentation_mask_coverage": candidate_frames == mask_frames,
            "separated_segment_mask_coverage": segment_frames == mask_frames,
            "identity_segment_one_to_one": sorted(segment_ids) == sorted(identity_ids),
            "timebase_roundtrip": len(timebase_rows) == len(result.separation.segments),
            "segments_within_audio": within_duration,
            "identity_matches_truth": not mismatched,
            "identity_labels_declared": not unknown_labels,
        },
        "counts": {
            "candidate_frames": len(candidate_frames),
            "mask_frames": len(mask_frames),
            "segments": len(segment_ids),
            "identities": len(identity_ids),
            "timebase_rows": len(timebase_rows),
        },
        "timebase_rows": timebase_rows,
        "issues": issues,
    }
    if issues and raise_on_error:
        raise MeetingError(
            "E_INCONSISTENT",
            "cross-module consistency self-check failed",
            {"issues": issues, "counts": report["counts"]},
        )
    return report
