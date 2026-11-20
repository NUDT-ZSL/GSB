from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import TrackingError
from .models import Detection


@dataclass
class PreprocessedSequence:
    seq_id: str
    revision: int
    frames: dict[int, tuple[Detection, ...]]
    truth_frames: dict[int, tuple[Detection, ...]]
    detections_by_track: dict[int, list[Detection]]
    truth_by_track: dict[int, list[Detection]]

    @property
    def frame_indices(self) -> list[int]:
        return sorted(self.frames)


@dataclass
class PreprocessedDataset:
    sequences: dict[str, PreprocessedSequence]
    detection_rows: list[dict[str, Any]]
    truth_rows: list[dict[str, Any]]

    @property
    def seq_ids(self) -> list[str]:
        return sorted(self.sequences)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_common(
    row: dict[str, Any],
    *,
    line_no: int,
    config: dict[str, Any],
    allow_missing_revision: bool,
) -> tuple[str, int, int, int]:
    required = (
        "seq_id",
        "frame_index",
        "track_id_true",
        "x1",
        "y1",
        "x2",
        "y2",
        "score",
        "class_id",
        "occluded",
    )
    missing = [field for field in required if field not in row]
    if missing:
        raise TrackingError(
            "E_INVALID_INPUT",
            f"missing required fields: {', '.join(missing)}",
            seq_id=str(row.get("seq_id")) if row.get("seq_id") is not None else None,
            frame_index=int(row["frame_index"]) if _is_int(row.get("frame_index")) else None,
            category="missing_field",
            context={"line": line_no, "fields": missing},
        )

    seq_id = row["seq_id"]
    if not isinstance(seq_id, str) or not seq_id.strip():
        raise TrackingError(
            "E_INVALID_INPUT",
            "seq_id must be a non-empty string",
            category="invalid_seq_id",
            context={"line": line_no},
        )
    frame_index = row["frame_index"]
    if not _is_int(frame_index) or frame_index < 0:
        raise TrackingError(
            "E_INVALID_INPUT",
            "frame_index must be a non-negative integer",
            seq_id=seq_id,
            category="invalid_frame_index",
            context={"line": line_no},
        )
    track_id_true = row["track_id_true"]
    if not _is_int(track_id_true) or track_id_true < 0:
        raise TrackingError(
            "E_INVALID_INPUT",
            "track_id_true must be a non-negative integer",
            seq_id=seq_id,
            frame_index=frame_index,
            category="invalid_track_id_true",
            context={"line": line_no},
        )
    revision_value = row.get("revision", 1)
    if not _is_int(revision_value) or revision_value < 1:
        raise TrackingError(
            "E_INVALID_INPUT",
            "revision must be a positive integer",
            seq_id=seq_id,
            frame_index=frame_index,
            category="invalid_revision",
            context={"line": line_no},
        )
    if not isinstance(row["occluded"], bool):
        raise TrackingError(
            "E_INVALID_OCCLUDED",
            "occluded must be a JSON boolean",
            seq_id=seq_id,
            frame_index=frame_index,
            category="occluded_type",
            context={"line": line_no, "value": row["occluded"]},
        )

    coordinates: dict[str, float] = {}
    for field in ("x1", "y1", "x2", "y2", "score"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            code = "E_INVALID_BBOX" if field != "score" else "E_INVALID_SCORE"
            raise TrackingError(
                code,
                f"{field} must be numeric",
                seq_id=seq_id,
                frame_index=frame_index,
                category="coordinate_type" if field != "score" else "score_type",
                context={"line": line_no, "field": field},
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise TrackingError(
                "E_INVALID_BBOX" if field != "score" else "E_INVALID_SCORE",
                f"{field} must be finite",
                seq_id=seq_id,
                frame_index=frame_index,
                category="non_finite" if field != "score" else "score_non_finite",
                context={"line": line_no, "field": field},
            )
        coordinates[field] = numeric

    if coordinates["x2"] <= coordinates["x1"] or coordinates["y2"] <= coordinates["y1"]:
        raise TrackingError(
            "E_INVALID_BBOX",
            "bbox must satisfy x1 < x2 and y1 < y2",
            seq_id=seq_id,
            frame_index=frame_index,
            category="degenerate_bbox",
            context={"line": line_no, "bbox": [row[field] for field in ("x1", "y1", "x2", "y2")]},
        )
    if coordinates["x1"] < 0 or coordinates["y1"] < 0:
        raise TrackingError(
            "E_INVALID_BBOX",
            "bbox coordinates must be non-negative",
            seq_id=seq_id,
            frame_index=frame_index,
            category="negative_coordinate",
            context={"line": line_no},
        )
    if not config["validation"]["allow_out_of_bounds"]:
        canvas_width, canvas_height = config["generator"]["canvas"]
        if coordinates["x2"] > float(canvas_width) or coordinates["y2"] > float(canvas_height):
            raise TrackingError(
                "E_INVALID_BBOX",
                "bbox exceeds configured canvas",
                seq_id=seq_id,
                frame_index=frame_index,
                category="out_of_bounds",
                context={
                    "line": line_no,
                    "bbox": [row[field] for field in ("x1", "y1", "x2", "y2")],
                    "canvas": [canvas_width, canvas_height],
                },
            )
    if not 0.0 <= coordinates["score"] <= 1.0:
        raise TrackingError(
            "E_INVALID_SCORE",
            "score must be in [0, 1]",
            seq_id=seq_id,
            frame_index=frame_index,
            category="score_range",
            context={"line": line_no, "score": coordinates["score"]},
        )
    if not _is_int(row["class_id"]) or row["class_id"] < 0:
        raise TrackingError(
            "E_INVALID_INPUT",
            "class_id must be a non-negative integer",
            seq_id=seq_id,
            frame_index=frame_index,
            category="invalid_class_id",
            context={"line": line_no},
        )

    appearance = row.get("appearance")
    if appearance is not None:
        if not isinstance(appearance, (list, tuple)) or len(appearance) != 3:
            raise TrackingError(
                "E_INVALID_INPUT",
                "appearance must contain exactly three numeric components",
                seq_id=seq_id,
                frame_index=frame_index,
                category="invalid_appearance",
                context={"line": line_no},
            )
        for component in appearance:
            if (
                isinstance(component, bool)
                or not isinstance(component, (int, float))
                or not math.isfinite(float(component))
            ):
                raise TrackingError(
                    "E_INVALID_INPUT",
                    "appearance components must be finite numbers",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="invalid_appearance",
                    context={"line": line_no},
                )

    return seq_id, frame_index, track_id_true, int(revision_value)


def _build_detection(row: dict[str, Any], revision: int) -> Detection:
    return Detection.from_dict(row, default_revision=revision)


def _check_frame_progression(
    seq_id: str,
    frame_indices: Iterable[int],
    config: dict[str, Any],
) -> None:
    frames = list(frame_indices)
    if not frames:
        raise TrackingError(
            "E_INVALID_INPUT",
            "sequence contains no detections",
            seq_id=seq_id,
            category="empty_sequence",
        )
    if config["validation"]["require_frame_zero"] and frames[0] != 0:
        raise TrackingError(
            "E_FRAME_GAP",
            f"sequence must start at frame 0, got {frames[0]}",
            seq_id=seq_id,
            frame_index=frames[0],
            category="missing_head_frame",
        )
    if config["validation"]["require_contiguous_frames"]:
        for previous, current in zip(frames, frames[1:]):
            if current == previous:
                raise TrackingError(
                    "E_FRAME_GAP",
                    f"duplicate frame block {current}",
                    seq_id=seq_id,
                    frame_index=current,
                    category="duplicate_frame",
                )
            if current != previous + 1:
                raise TrackingError(
                    "E_FRAME_GAP",
                    f"frame jump from {previous} to {current}",
                    seq_id=seq_id,
                    frame_index=current,
                    category="jump",
                    context={"previous_frame": previous},
                )


def _group_rows(
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    kind: str,
) -> tuple[
    dict[str, int],
    dict[str, dict[int, list[dict[str, Any]]]],
    dict[str, int],
]:
    revisions: dict[str, int] = {}
    grouped: dict[str, dict[int, list[dict[str, Any]]]] = {}
    line_numbers: dict[str, int] = {}
    last_seq: str | None = None
    seen_sequence_blocks: set[str] = set()

    for index, row in enumerate(rows):
        line_no = index + 1
        seq_id, frame_index, _track_id, revision = _validate_common(
            row,
            line_no=line_no,
            config=config,
            allow_missing_revision=True,
        )
        if last_seq is not None and seq_id != last_seq:
            if seq_id in seen_sequence_blocks:
                raise TrackingError(
                    "E_DUPLICATE_SEQ_ID",
                    f"seq_id '{seq_id}' appears in more than one contiguous block",
                    seq_id=seq_id,
                    frame_index=frame_index,
                    category="repeated_sequence_block",
                    context={"line": line_no, "kind": kind},
                )
            seen_sequence_blocks.add(last_seq)
        elif last_seq is None:
            seen_sequence_blocks.add(seq_id)
        if last_seq == seq_id and seq_id not in seen_sequence_blocks:
            seen_sequence_blocks.add(seq_id)

        if seq_id in revisions and revisions[seq_id] != revision:
            raise TrackingError(
                "E_INVALID_INPUT",
                "a sequence block must use one revision",
                seq_id=seq_id,
                frame_index=frame_index,
                category="mixed_revision",
                context={"line": line_no},
            )
        revisions[seq_id] = revision
        grouped.setdefault(seq_id, {}).setdefault(frame_index, []).append(row)
        line_numbers[f"{seq_id}:{frame_index}:{len(grouped[seq_id][frame_index])}"] = line_no
        last_seq = seq_id

    return revisions, grouped, line_numbers


def preprocess_dataset(
    detection_rows: list[dict[str, Any]],
    truth_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> PreprocessedDataset:
    if not detection_rows:
        raise TrackingError("E_INVALID_INPUT", "detection input is empty", category="empty_input")
    if not truth_rows:
        raise TrackingError("E_INVALID_INPUT", "truth input is empty", category="empty_truth")

    det_revisions, grouped, _ = _group_rows(
        detection_rows, config=config, kind="detections"
    )
    truth_revisions, truth_grouped, _ = _group_rows(
        truth_rows, config=config, kind="truth"
    )
    if set(grouped) != set(truth_grouped):
        missing_truth = sorted(set(grouped) - set(truth_grouped))
        missing_det = sorted(set(truth_grouped) - set(grouped))
        raise TrackingError(
            "E_INCONSISTENT",
            "detection and truth sequence sets differ",
            category="sequence_set_mismatch",
            context={"missing_truth": missing_truth, "missing_detections": missing_det},
        )

    sequences: dict[str, PreprocessedSequence] = {}
    for seq_id in sorted(grouped):
        revision = det_revisions[seq_id]
        if truth_revisions[seq_id] != revision:
            raise TrackingError(
                "E_REVISION_INVALID",
                "detection and truth revisions differ",
                seq_id=seq_id,
                category="detection_truth_revision_mismatch",
                context={
                    "detection_revision": revision,
                    "truth_revision": truth_revisions[seq_id],
                },
            )
        frames_raw = grouped[seq_id]
        expected_frames = sorted(frames_raw)
        _check_frame_progression(seq_id, expected_frames, config)
        frames: dict[int, tuple[Detection, ...]] = {}
        by_track: dict[int, list[Detection]] = {}
        seen_detection_keys: set[tuple[int, int]] = set()
        for frame_index in expected_frames:
            frame_rows = sorted(
                frames_raw[frame_index],
                key=lambda row: (int(row["track_id_true"]), -float(row["score"])),
            )
            detections: list[Detection] = []
            for row in frame_rows:
                key = (frame_index, int(row["track_id_true"]))
                if key in seen_detection_keys:
                    raise TrackingError(
                        "E_DUPLICATE_DETECTION",
                        "duplicate detection for the same seq/frame/true track",
                        seq_id=seq_id,
                        frame_index=frame_index,
                        category="duplicate_detection",
                        context={"track_id_true": key[1]},
                    )
                seen_detection_keys.add(key)
                detection = _build_detection(row, revision)
                detections.append(detection)
                by_track.setdefault(detection.track_id_true, []).append(detection)
            frames[frame_index] = tuple(detections)

        truth_frames_raw = truth_grouped[seq_id]
        truth_expected = sorted(truth_frames_raw)
        _check_frame_progression(seq_id, truth_expected, config)
        truth_frames: dict[int, tuple[Detection, ...]] = {}
        truth_by_track: dict[int, list[Detection]] = {}
        seen_truth_keys: set[tuple[int, int]] = set()
        for frame_index in truth_expected:
            frame_rows = sorted(
                truth_frames_raw[frame_index],
                key=lambda row: (int(row["track_id_true"]), -float(row["score"])),
            )
            detections: list[Detection] = []
            for row in frame_rows:
                key = (frame_index, int(row["track_id_true"]))
                if key in seen_truth_keys:
                    raise TrackingError(
                        "E_DUPLICATE_DETECTION",
                        "duplicate truth object for the same seq/frame/true track",
                        seq_id=seq_id,
                        frame_index=frame_index,
                        category="duplicate_truth",
                    )
                seen_truth_keys.add(key)
                truth = _build_detection(row, revision)
                detections.append(truth)
                truth_by_track.setdefault(truth.track_id_true, []).append(truth)
            truth_frames[frame_index] = tuple(detections)

        sequences[seq_id] = PreprocessedSequence(
            seq_id=seq_id,
            revision=revision,
            frames=frames,
            truth_frames=truth_frames,
            detections_by_track=by_track,
            truth_by_track=truth_by_track,
        )

    return PreprocessedDataset(
        sequences=sequences,
        detection_rows=detection_rows,
        truth_rows=truth_rows,
    )
