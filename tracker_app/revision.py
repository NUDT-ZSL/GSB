from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .jsonio import canonical_json


DETECTION_DIFF_FIELDS = (
    "x1",
    "y1",
    "x2",
    "y2",
    "score",
    "class_id",
    "occluded",
    "appearance",
)
TRUTH_DIFF_FIELDS = DETECTION_DIFF_FIELDS + ("visible",)


@dataclass
class RevisionDiff:
    affected_frames: list[int]
    affected_truth_tracks: list[int]
    changed_detection_frames: list[int]
    changed_truth_frames: list[int]
    changes: list[dict[str, Any]]

    @property
    def earliest_frame(self) -> int | None:
        return self.affected_frames[0] if self.affected_frames else None


def _index_rows(
    rows: Iterable[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[tuple[int, int], dict[str, Any]]:
    indexed: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (int(row["frame_index"]), int(row["track_id_true"]))
        indexed[key] = {field: row.get(field) for field in fields}
    return indexed


def diff_revision_inputs(
    old_detections: list[dict[str, Any]],
    new_detections: list[dict[str, Any]],
    old_truth: list[dict[str, Any]],
    new_truth: list[dict[str, Any]],
) -> RevisionDiff:
    old_detection_index = _index_rows(old_detections, DETECTION_DIFF_FIELDS)
    new_detection_index = _index_rows(new_detections, DETECTION_DIFF_FIELDS)
    old_truth_index = _index_rows(old_truth, TRUTH_DIFF_FIELDS)
    new_truth_index = _index_rows(new_truth, TRUTH_DIFF_FIELDS)

    changed_detection_frames: set[int] = set()
    changed_truth_frames: set[int] = set()
    affected_truth: set[int] = set()
    changes: list[dict[str, Any]] = []
    all_detection_keys = sorted(set(old_detection_index) | set(new_detection_index))
    for frame_index, truth_id in all_detection_keys:
        old = old_detection_index.get((frame_index, truth_id))
        new = new_detection_index.get((frame_index, truth_id))
        if old == new:
            continue
        changed_detection_frames.add(frame_index)
        affected_truth.add(truth_id)
        changes.append(
            {
                "kind": "detection",
                "frame_index": frame_index,
                "track_id_true": truth_id,
                "change": (
                    "added"
                    if old is None
                    else "removed"
                    if new is None
                    else "modified"
                ),
                "old": old,
                "new": new,
            }
        )

    all_truth_keys = sorted(set(old_truth_index) | set(new_truth_index))
    for frame_index, truth_id in all_truth_keys:
        old = old_truth_index.get((frame_index, truth_id))
        new = new_truth_index.get((frame_index, truth_id))
        if old == new:
            continue
        changed_truth_frames.add(frame_index)
        affected_truth.add(truth_id)
        changes.append(
            {
                "kind": "truth",
                "frame_index": frame_index,
                "track_id_true": truth_id,
                "change": (
                    "added"
                    if old is None
                    else "removed"
                    if new is None
                    else "modified"
                ),
                "old": old,
                "new": new,
            }
        )

    affected_frames = sorted(changed_detection_frames | changed_truth_frames)
    changes.sort(
        key=lambda item: (
            item["frame_index"],
            item["kind"],
            item["track_id_true"],
        )
    )
    return RevisionDiff(
        affected_frames=affected_frames,
        affected_truth_tracks=sorted(affected_truth),
        changed_detection_frames=sorted(changed_detection_frames),
        changed_truth_frames=sorted(changed_truth_frames),
        changes=changes,
    )


def affected_prediction_tracks(
    old_checkpoints: Iterable[dict[str, Any]],
    affected_frames: Iterable[int],
    affected_truth_tracks: Iterable[int],
) -> list[int]:
    frames = set(int(frame) for frame in affected_frames)
    truth_ids = set(int(track) for track in affected_truth_tracks)
    predicted: set[int] = set()
    for checkpoint in old_checkpoints:
        if int(checkpoint["frame_index"]) not in frames:
            continue
        for row in checkpoint.get("assignments", []):
            if (
                row.get("pred_track_id") is not None
                and int(row["track_id_true"]) in truth_ids
            ):
                predicted.add(int(row["pred_track_id"]))
    return sorted(predicted)


def revision_changes_checksum(changes: list[dict[str, Any]]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(changes).encode("utf-8")).hexdigest()
