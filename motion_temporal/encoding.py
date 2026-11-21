"""Deterministic motion encoding from frame pairs, pixels and sparse masks."""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

import numpy as np

from .canonical import canonical_bytes, r6, sha256_bytes
from .codes import E_MASK_SHAPE, E_OBJECT_OVERFLOW
from .errors import BusinessError
from .models import ClipSpec, FrameSample, TargetObservation


def _mask_centroid(target: TargetObservation, image: np.ndarray | None) -> tuple[float, float] | None:
    if target.occluded or not target.mask_coords:
        return None
    weighted_x = 0.0
    weighted_y = 0.0
    total = 0.0
    for y, x in target.mask_coords:
        weight = float(image[y, x]) if image is not None and 0 <= y < image.shape[0] and 0 <= x < image.shape[1] else 1.0
        if weight <= 0.0:
            weight = 1.0
        weighted_x += x * weight
        weighted_y += y * weight
        total += weight
    if total <= 0.0:
        return None
    return (weighted_x / total, weighted_y / total)


def _validate_targets(
    spec: ClipSpec,
    frame: FrameSample,
    observed_ids: set[int],
) -> None:
    if len(frame.targets) > spec.object_count:
        raise BusinessError(
            E_OBJECT_OVERFLOW,
            f"target count {len(frame.targets)} exceeds object_count {spec.object_count}",
            spec.clip_id,
            frame.frame_index,
            {"object_count": spec.object_count, "target_count": len(frame.targets)},
        )
    for target in frame.targets:
        if target.object_id < 0 or target.object_id >= spec.object_count:
            raise BusinessError(
                E_OBJECT_OVERFLOW,
                f"object_id {target.object_id} outside [0, {spec.object_count - 1}]",
                spec.clip_id,
                frame.frame_index,
                {"field": "object_id", "value": target.object_id},
            )
        if target.object_id in observed_ids:
            raise BusinessError(
                E_OBJECT_OVERFLOW,
                f"duplicate object_id {target.object_id}",
                spec.clip_id,
                frame.frame_index,
                {"field": "object_id", "value": target.object_id},
            )
        observed_ids.add(target.object_id)
        if target.mask_shape != (spec.canvas_height, spec.canvas_width):
            raise BusinessError(
                E_MASK_SHAPE,
                f"mask shape {target.mask_shape} != canvas {(spec.canvas_height, spec.canvas_width)}",
                spec.clip_id,
                frame.frame_index,
                {
                    "object_id": target.object_id,
                    "expected_shape": [spec.canvas_height, spec.canvas_width],
                    "actual_shape": list(target.mask_shape),
                },
            )
        for y, x in target.mask_coords:
            if not (0 <= y < spec.canvas_height and 0 <= x < spec.canvas_width):
                raise BusinessError(
                    E_MASK_SHAPE,
                    f"mask coordinate ({y}, {x}) outside canvas",
                    spec.clip_id,
                    frame.frame_index,
                    {"object_id": target.object_id, "coordinate": [y, x]},
                )


def _empty_object(object_id: int, status: str, box: list[float] | None = None) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "status": status,
        "box": box,
        "displacement": None,
        "velocity": None,
        "speed": None,
        "acceleration": None,
        "extrapolation_confidence": 0.0,
        "mask_support": 0,
        "last_valid_frame": None,
    }


def _observed_object(
    target: TargetObservation,
    displacement: tuple[float, float] | None,
    dt_seconds: float,
    previous_velocity: tuple[float, float] | None,
    previous_valid_frame: int | None,
    frame_index: int,
    mask_support: int,
    acceleration_scale: float,
    acceleration_dt_seconds: float,
) -> dict[str, Any]:
    if displacement is None:
        return _empty_object(target.object_id, "missing", [r6(value) for value in target.box])
    vx = displacement[0] / dt_seconds
    vy = displacement[1] / dt_seconds
    if previous_velocity is not None and previous_valid_frame is not None:
        ax = (vx - previous_velocity[0]) / acceleration_dt_seconds
        ay = (vy - previous_velocity[1]) / acceleration_dt_seconds
    else:
        ax = 0.0
        ay = 0.0
    acceleration_magnitude = math.hypot(ax, ay)
    support_factor = min(1.0, mask_support / 10.0)
    confidence = support_factor / (1.0 + acceleration_magnitude / max(acceleration_scale, 1e-9))
    return {
        "object_id": target.object_id,
        "status": "observed",
        "box": [r6(value) for value in target.box],
        "displacement": [r6(displacement[0]), r6(displacement[1])],
        "velocity": [r6(vx), r6(vy)],
        "speed": r6(math.hypot(vx, vy)),
        "acceleration": [r6(ax), r6(ay)],
        "extrapolation_confidence": r6(max(0.0, min(1.0, confidence))),
        "mask_support": int(mask_support),
        "last_valid_frame": frame_index,
    }


def encode_motion(
    spec: ClipSpec,
    frames: Iterable[FrameSample],
    sampling_payload: dict[str, Any],
    *,
    acceleration_scale: float = 300.0,
    frame_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Encode pair displacements and per-object trajectory features per frame."""

    frame_map = {frame.frame_index: frame for frame in frames}
    pair_map: dict[int, dict[str, Any]] = {}
    for pair in sampling_payload.get("frame_pairs", []):
        destination = int(pair["dst_frame_index"])
        current = pair_map.get(destination)
        if current is None or (pair["delta_ms"], pair["src_frame_index"]) < (
            current["delta_ms"],
            current["src_frame_index"],
        ):
            pair_map[destination] = pair

    previous_velocity: dict[int, tuple[float, float]] = {}
    previous_valid_frame: dict[int, int] = {}
    previous_box: dict[int, list[float]] = {}
    previous_box_frame: dict[int, int] = {}
    records: list[dict[str, Any]] = []
    dt_seconds = spec.frame_interval_ms / 1000.0

    for frame_index in range(spec.frame_count):
        if frame_callback is not None:
            frame_callback(frame_index)
        if frame_index not in frame_map:
            raise BusinessError(
                E_MASK_SHAPE,
                f"frame_index {frame_index} missing from encoding input",
                spec.clip_id,
                frame_index,
            )
        frame = frame_map[frame_index]
        observed_ids: set[int] = set()
        _validate_targets(spec, frame, observed_ids)
        by_object = {target.object_id: target for target in frame.targets}
        objects: list[dict[str, Any]] = []
        pair = pair_map.get(frame_index)
        source_frame: FrameSample | None = frame_map.get(int(pair["src_frame_index"])) if pair is not None else None
        source_targets = {target.object_id: target for target in source_frame.targets} if source_frame else {}

        for object_id in range(spec.object_count):
            target = by_object.get(object_id)
            if frame_index == 0:
                if target is None or not frame.observed:
                    objects.append(_empty_object(object_id, "missing"))
                    continue
                if target.occluded:
                    placeholder_box = [r6(value) for value in target.box]
                    objects.append(_empty_object(object_id, "occluded", placeholder_box))
                    previous_box[object_id] = placeholder_box
                    previous_box_frame[object_id] = 0
                    continue
                objects.append(
                    {
                        "object_id": object_id,
                        "status": "observed",
                        "box": [r6(value) for value in target.box],
                        "displacement": [0.0, 0.0],
                        "velocity": [0.0, 0.0],
                        "speed": 0.0,
                        "acceleration": [0.0, 0.0],
                        "extrapolation_confidence": 1.0,
                        "mask_support": len(target.mask_coords),
                        "last_valid_frame": 0,
                    }
                )
                previous_velocity[object_id] = (0.0, 0.0)
                previous_valid_frame[object_id] = 0
                previous_box[object_id] = [r6(value) for value in target.box]
                previous_box_frame[object_id] = 0
                continue

            if not frame.observed:
                for missing_id in range(spec.object_count):
                    objects.append(_empty_object(missing_id, "missing", previous_box.get(missing_id)))
                break

            if target is None:
                objects.append(_empty_object(object_id, "missing", previous_box.get(object_id)))
                continue
            if target.occluded or not target.mask_coords:
                objects.append(_empty_object(object_id, "occluded", [r6(value) for value in target.box]))
                previous_box[object_id] = [r6(value) for value in target.box]
                previous_box_frame[object_id] = frame_index
                continue

            source_target = source_targets.get(object_id)
            displacement: tuple[float, float] | None = None
            if source_target is not None and not source_target.occluded:
                source_center = _mask_centroid(source_target, source_frame.image if source_frame else None)
                destination_center = _mask_centroid(target, frame.image)
                if source_center is not None and destination_center is not None:
                    displacement = (
                        destination_center[0] - source_center[0],
                        destination_center[1] - source_center[1],
                    )
            displacement_dt_seconds = (
                dt_seconds * (pair["delta_ms"] / spec.frame_interval_ms) if pair is not None else dt_seconds
            )
            if displacement is None and object_id in previous_box:
                prior_box = previous_box[object_id]
                prior_center = ((prior_box[0] + prior_box[2]) / 2.0, (prior_box[1] + prior_box[3]) / 2.0)
                current_center = ((target.box[0] + target.box[2]) / 2.0, (target.box[1] + target.box[3]) / 2.0)
                displacement = (
                    current_center[0] - prior_center[0],
                    current_center[1] - prior_center[1],
                )
                displacement_dt_seconds = max(
                    dt_seconds,
                    (frame_index - previous_box_frame.get(object_id, frame_index - 1)) * dt_seconds,
                )
            acceleration_gap = max(1, frame_index - previous_valid_frame.get(object_id, frame_index - 1))
            acceleration_dt_seconds = max(1e-9, acceleration_gap * dt_seconds)
            encoded = _observed_object(
                target,
                displacement,
                displacement_dt_seconds,
                previous_velocity.get(object_id),
                previous_valid_frame.get(object_id),
                frame_index,
                len(target.mask_coords),
                acceleration_scale,
                acceleration_dt_seconds,
            )
            if encoded["status"] == "observed":
                previous_velocity[object_id] = (
                    float(encoded["velocity"][0]),
                    float(encoded["velocity"][1]),
                )
                previous_valid_frame[object_id] = frame_index
                previous_box[object_id] = list(encoded["box"])
                previous_box_frame[object_id] = frame_index
            objects.append(encoded)

        records.append(
            {
                "clip_id": spec.clip_id,
                "frame_index": frame_index,
                "timestamp_ms": frame.timestamp_ms,
                "observed": frame.observed,
                "pair_ref": (
                    {
                        "pair_id": pair["pair_id"],
                        "src_frame_index": int(pair["src_frame_index"]),
                        "dst_frame_index": int(pair["dst_frame_index"]),
                        "delta_ms": int(pair["delta_ms"]),
                        "src_timestamp_ms": int(pair["src_timestamp_ms"]),
                        "dst_timestamp_ms": int(pair["dst_timestamp_ms"]),
                    }
                    if pair is not None
                    else None
                ),
                "objects": objects,
            }
        )

    payload: dict[str, Any] = {
        "clip_id": spec.clip_id,
        "object_count": spec.object_count,
        "frame_indices": [record["frame_index"] for record in records],
        "frames": records,
    }
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload

