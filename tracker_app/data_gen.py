from __future__ import annotations

import copy
import math
import random
from pathlib import Path
from typing import Any, Iterable

from .jsonio import atomic_write_json, atomic_write_jsonl, canonical_json, sha256_bytes


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _clamp_bbox(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
    canvas_width: float,
    canvas_height: float,
) -> tuple[float, float, float, float]:
    half_w = width / 2.0
    half_h = height / 2.0
    center_x = min(max(center_x, half_w + 2.0), canvas_width - half_w - 2.0)
    center_y = min(max(center_y, half_h + 2.0), canvas_height - half_h - 2.0)
    return (
        _round(center_x - half_w),
        _round(center_y - half_h),
        _round(center_x + half_w),
        _round(center_y + half_h),
    )


def _make_targets(
    rng: random.Random,
    *,
    target_count: int,
    canvas: tuple[float, float],
    crossing_frame: int,
) -> list[dict[str, Any]]:
    width, height = canvas
    targets: list[dict[str, Any]] = []
    palette = [
        (0.88, 0.22, 0.18),
        (0.12, 0.48, 0.90),
        (0.13, 0.72, 0.35),
        (0.82, 0.58, 0.10),
        (0.58, 0.20, 0.82),
        (0.10, 0.70, 0.72),
    ]
    for target_index in range(target_count):
        target_width = rng.uniform(38.0, 54.0)
        target_height = rng.uniform(52.0, 78.0)
        start_x = rng.uniform(target_width * 0.7, width - target_width * 0.7)
        start_y = rng.uniform(target_height * 0.7, height - target_height * 0.7)
        vx = rng.uniform(-2.1, 2.1)
        vy = rng.uniform(-0.8, 0.8)

        if target_index == 0 and target_count >= 2:
            start_x = width * 0.16
            start_y = height * 0.44
            vx = ((width * 0.47) - start_x) / max(crossing_frame, 1)
            vy = 0.35
        elif target_index == 1 and target_count >= 2:
            start_x = width * 0.84
            start_y = height * 0.50
            vx = ((width * 0.53) - start_x) / max(crossing_frame, 1)
            vy = -0.35

        color = palette[target_index % len(palette)]
        jitter = [rng.uniform(-0.035, 0.035) for _ in range(3)]
        appearance = tuple(
            _round(max(0.0, min(1.0, color[channel] + jitter[channel])), 6)
            for channel in range(3)
        )
        targets.append(
            {
                "true_id": target_index,
                "center_x": start_x,
                "center_y": start_y,
                "width": target_width,
                "height": target_height,
                "vx": vx,
                "vy": vy,
                "appearance": appearance,
                "class_id": target_index % 2,
            }
        )
    return targets


def _step_target(
    target: dict[str, Any],
    canvas: tuple[float, float],
) -> None:
    width, height = canvas
    target["center_x"] += target["vx"]
    target["center_y"] += target["vy"]
    half_w = target["width"] / 2.0
    half_h = target["height"] / 2.0
    if target["center_x"] < half_w + 2.0 or target["center_x"] > width - half_w - 2.0:
        target["vx"] *= -1.0
        target["center_x"] = min(
            max(target["center_x"], half_w + 2.0), width - half_w - 2.0
        )
    if target["center_y"] < half_h + 2.0 or target["center_y"] > height - half_h - 2.0:
        target["vy"] *= -1.0
        target["center_y"] = min(
            max(target["center_y"], half_h + 2.0), height - half_h - 2.0
        )


def _inside(interval: Iterable[int], frame_index: int) -> bool:
    start, end = interval
    return int(start) <= frame_index <= int(end)


def generate_dataset(config: dict[str, Any]) -> dict[str, Any]:
    """Generate deterministic detections and full-frame truth.

    The generated "world" is independent of any detector noise. Detections are
    omitted only during configured short-disappearance intervals; occlusion
    keeps the box but changes score and the occluded flag.
    """

    gen = config["generator"]
    sequence_count = int(gen["sequence_count"])
    frame_count = int(gen["frames"])
    target_count = int(gen["targets_per_frame"])
    canvas = (float(gen["canvas"][0]), float(gen["canvas"][1]))
    base_seed = int(gen["seed"])
    revision = int(gen.get("revision", 1))
    crossing_frame = int(gen.get("crossing_frame", frame_count // 2))
    occlusion_intervals = [tuple(map(int, item)) for item in gen["occlusion_intervals"]]
    disappearance_intervals = [
        tuple(map(int, item)) for item in gen["disappearance_intervals"]
    ]

    detections: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []
    sequences: list[dict[str, Any]] = []

    for sequence_index in range(sequence_count):
        seq_id = f"seq-{sequence_index:03d}"
        sequence_seed = base_seed + sequence_index * 1009
        rng = random.Random(sequence_seed)
        targets = _make_targets(
            rng,
            target_count=target_count,
            canvas=canvas,
            crossing_frame=crossing_frame,
        )
        sequence_detection_count = 0
        sequence_missing_count = 0
        for frame_index in range(frame_count):
            if frame_index > 0:
                for target in targets:
                    _step_target(target, canvas)

            for target in targets:
                true_id = int(target["true_id"])
                disappeared = any(
                    _inside(interval, frame_index)
                    and (true_id == 0 or (true_id + sequence_index) % 3 == 0)
                    for interval in disappearance_intervals
                )
                occluded = any(
                    _inside(interval, frame_index)
                    and (true_id + sequence_index + frame_index) % 2 == 0
                    for interval in occlusion_intervals
                )
                if true_id in (0, 1) and frame_index == crossing_frame:
                    occluded = True

                bbox = _clamp_bbox(
                    target["center_x"],
                    target["center_y"],
                    target["width"],
                    target["height"],
                    canvas[0],
                    canvas[1],
                )
                truth_row = {
                    "seq_id": seq_id,
                    "revision": revision,
                    "frame_index": frame_index,
                    "track_id_true": true_id,
                    "x1": bbox[0],
                    "y1": bbox[1],
                    "x2": bbox[2],
                    "y2": bbox[3],
                    "score": 1.0,
                    "class_id": int(target["class_id"]),
                    "occluded": bool(occluded),
                    "visible": not disappeared,
                    "appearance": list(target["appearance"]),
                }
                truth.append(truth_row)
                if disappeared:
                    sequence_missing_count += 1
                    continue

                jitter_seed = (
                    base_seed * 1_000_003
                    + sequence_index * 10_007
                    + frame_index * 101
                    + true_id
                )
                score_rng = random.Random(jitter_seed)
                base_score = 0.86 if not occluded else 0.57
                score = _round(
                    max(0.05, min(0.99, base_score + score_rng.uniform(-0.025, 0.025))),
                    6,
                )
                bbox_noise = 0.0 if not occluded else score_rng.uniform(0.0, 1.2)
                x1 = _round(max(0.0, bbox[0] - bbox_noise))
                y1 = _round(max(0.0, bbox[1] - bbox_noise))
                x2 = _round(min(canvas[0], bbox[2] + bbox_noise))
                y2 = _round(min(canvas[1], bbox[3] + bbox_noise))
                detections.append(
                    {
                        "seq_id": seq_id,
                        "revision": revision,
                        "frame_index": frame_index,
                        "track_id_true": true_id,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "score": score,
                        "class_id": int(target["class_id"]),
                        "occluded": bool(occluded),
                        "appearance": list(target["appearance"]),
                    }
                )
                sequence_detection_count += 1

        sequences.append(
            {
                "seq_id": seq_id,
                "revision": revision,
                "seed": sequence_seed,
                "frames": frame_count,
                "targets_per_frame": target_count,
                "canvas": [int(canvas[0]), int(canvas[1])],
                "detections": sequence_detection_count,
                "missing_detections": sequence_missing_count,
                "occlusion_intervals": [list(item) for item in occlusion_intervals],
                "disappearance_intervals": [
                    list(item) for item in disappearance_intervals
                ],
                "crossing_frame": crossing_frame,
            }
        )

    detections.sort(key=lambda row: (row["seq_id"], row["frame_index"], row["track_id_true"]))
    truth.sort(key=lambda row: (row["seq_id"], row["frame_index"], row["track_id_true"]))
    manifest = {
        "schema": "mt-dataset-v1",
        "revision": revision,
        "seed": base_seed,
        "sequences": sequences,
        "detection_count": len(detections),
        "truth_count": len(truth),
        "detections_sha256": sha256_bytes(
            "".join(canonical_json(item) + "\n" for item in detections).encode("utf-8")
        ),
        "truth_sha256": sha256_bytes(
            "".join(canonical_json(item) + "\n" for item in truth).encode("utf-8")
        ),
    }
    return {
        "detections": detections,
        "truth": truth,
        "manifest": manifest,
    }


def write_dataset(dataset: dict[str, Any], output_dir: str | Path) -> Path:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(target / "detections.jsonl", dataset["detections"])
    atomic_write_jsonl(target / "truth.jsonl", dataset["truth"])
    atomic_write_json(target / "manifest.json", dataset["manifest"], pretty=True)
    return target


def mutate_detection(
    dataset: dict[str, Any],
    *,
    seq_id: str,
    frame_index: int,
    track_id_true: int | None = None,
    changes: dict[str, Any],
    revision: int | None = None,
) -> dict[str, Any]:
    """Return a deep-copied dataset with one deterministic revision patch."""

    result = copy.deepcopy(dataset)
    changed = 0
    for row in result["detections"]:
        if revision is not None:
            row["revision"] = revision
        if row["seq_id"] != seq_id or row["frame_index"] != frame_index:
            continue
        if track_id_true is not None and row["track_id_true"] != track_id_true:
            continue
        row.update(copy.deepcopy(changes))
        changed += 1
    if changed == 0:
        raise ValueError("revision patch did not match any detection")
    if revision is not None:
        for row in result["truth"]:
            row["revision"] = revision
        result["manifest"]["revision"] = revision
        for sequence in result["manifest"]["sequences"]:
            sequence["revision"] = revision
    result["manifest"]["detection_count"] = len(result["detections"])
    result["manifest"]["truth_count"] = len(result["truth"])
    result["manifest"]["detections_sha256"] = sha256_bytes(
        "".join(canonical_json(item) + "\n" for item in result["detections"]).encode(
            "utf-8"
        )
    )
    result["manifest"]["truth_sha256"] = sha256_bytes(
        "".join(canonical_json(item) + "\n" for item in result["truth"]).encode("utf-8")
    )
    return result


def make_error_fixtures(
    dataset: dict[str, Any],
    fixture_dir: str | Path,
) -> dict[str, dict[str, Any]]:
    target = Path(fixture_dir)
    target.mkdir(parents=True, exist_ok=True)
    detections = dataset["detections"]
    first_seq = detections[0]["seq_id"]
    other_seq = next(
        (row["seq_id"] for row in detections if row["seq_id"] != first_seq),
        first_seq,
    )

    duplicate = copy.deepcopy(detections)
    first_block = [copy.deepcopy(row) for row in detections if row["seq_id"] == first_seq]
    insertion = [
        copy.deepcopy(row) for row in detections if row["seq_id"] == other_seq
    ]
    duplicate = first_block + insertion + first_block

    gap = copy.deepcopy(detections)
    seq_rows = [row for row in gap if row["seq_id"] == first_seq]
    available_frames = sorted({row["frame_index"] for row in seq_rows})
    gap_frame = available_frames[len(available_frames) // 2]
    gap = [
        row
        for row in gap
        if not (row["seq_id"] == first_seq and row["frame_index"] == gap_frame)
    ]

    bad_bbox = copy.deepcopy(detections)
    target_frame = available_frames[max(1, len(available_frames) // 3)]
    for row in bad_bbox:
        if row["seq_id"] == first_seq and row["frame_index"] == target_frame:
            row["x2"] = row["x1"]
            break

    bad_occluded = copy.deepcopy(detections)
    for row in bad_occluded:
        if row["seq_id"] == first_seq and row["frame_index"] == target_frame:
            row["occluded"] = "sometimes"
            break

    fixtures = {
        "bad_duplicate_seq": (duplicate, "duplicate sequence block"),
        "bad_frame_gap": (gap, "removed an interior frame"),
        "bad_bbox": (bad_bbox, "x2 equals x1"),
        "bad_occluded": (bad_occluded, "occluded is a string"),
    }
    descriptions: dict[str, dict[str, Any]] = {}
    for name, (rows, description) in fixtures.items():
        path = target / f"{name}.jsonl"
        atomic_write_jsonl(path, rows)
        descriptions[name] = {
            "path": str(path),
            "description": description,
            "seq_id": first_seq,
            "frame_index": (
                target_frame if name in {"bad_bbox", "bad_occluded"} else gap_frame
            ),
            "expected_error": {
                "bad_duplicate_seq": "E_DUPLICATE_SEQ_ID",
                "bad_frame_gap": "E_FRAME_GAP",
                "bad_bbox": "E_INVALID_BBOX",
                "bad_occluded": "E_INVALID_OCCLUDED",
            }[name],
        }
    atomic_write_json(target / "fixtures.json", descriptions, pretty=True)
    return descriptions
