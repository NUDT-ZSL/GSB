"""Recursive temporal aggregation with an explicit, hashed history window."""

from __future__ import annotations

import math
from typing import Any, Callable

from .canonical import canonical_bytes, r6, sha256_bytes
from .codes import E_DECAY_INVALID, E_EXTRAPOLATION_OOB, E_WINDOW_INVALID
from .errors import BusinessError
from .masks import make_predicted_mask
from .models import Box, ClipSpec


def _safe_float(value: Any, default: float = 0.0) -> float:
    return float(default if value is None else value)


def _box_center(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _window_digest(entries: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical_bytes(entries))


def _window_velocity(entries: list[dict[str, Any]], object_id: int) -> tuple[float, float] | None:
    values: list[tuple[float, float]] = []
    for entry in entries:
        for item in entry.get("objects", []):
            if int(item.get("object_id", -1)) != object_id:
                continue
            velocity = item.get("velocity")
            if velocity is not None:
                values.append((float(velocity[0]), float(velocity[1])))
    if not values:
        return None
    total_weight = 0.0
    weighted_x = 0.0
    weighted_y = 0.0
    for index, (vx, vy) in enumerate(values):
        weight = float(index + 1)
        weighted_x += vx * weight
        weighted_y += vy * weight
        total_weight += weight
    return (weighted_x / total_weight, weighted_y / total_weight)


def aggregate_temporal(
    spec: ClipSpec,
    encoding_payload: dict[str, Any],
    *,
    window_length: int = 4,
    decay: float = 0.85,
    max_history_windows: int | None = None,
    extrapolation_margin: float = 96.0,
    frame_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Predict every frame recursively and attach history/state checksums."""

    if not isinstance(window_length, int) or window_length < 1:
        raise BusinessError(
            E_WINDOW_INVALID,
            f"window_length must be an integer >= 1, got {window_length!r}",
            spec.clip_id,
            0,
            {"window_length": window_length},
        )
    if max_history_windows is not None and window_length > max_history_windows:
        raise BusinessError(
            E_WINDOW_INVALID,
            f"window_length {window_length} exceeds max_history_windows {max_history_windows}",
            spec.clip_id,
            0,
            {"window_length": window_length, "max_history_windows": max_history_windows},
        )
    if not math.isfinite(float(decay)) or float(decay) <= 0.0:
        raise BusinessError(
            E_DECAY_INVALID,
            f"decay must be finite and > 0, got {decay!r}",
            spec.clip_id,
            0,
            {"decay": decay},
        )

    encoded_frames = {int(record["frame_index"]): record for record in encoding_payload.get("frames", [])}
    expected = set(range(spec.frame_count))
    if set(encoded_frames) != expected:
        missing = sorted(expected - set(encoded_frames))
        raise BusinessError(
            E_WINDOW_INVALID,
            "encoding frame set does not match clip frame_count",
            spec.clip_id,
            missing[0] if missing else spec.frame_count - 1,
            {"missing_frames": missing},
        )

    dt_seconds = spec.frame_interval_ms / 1000.0
    states: dict[int, dict[str, Any]] = {}
    last_observed: dict[int, int] = {}
    history: list[dict[str, Any]] = []
    output_frames: list[dict[str, Any]] = []
    effective_decay = r6(decay)

    for frame_index in range(spec.frame_count):
        if frame_callback is not None:
            frame_callback(frame_index)
        encoded = encoded_frames[frame_index]
        history_entries = history[-window_length:]
        history_summary = {
            "window_length": window_length,
            "start_frame": history_entries[0]["frame_index"] if history_entries else 0,
            "end_frame": history_entries[-1]["frame_index"] if history_entries else -1,
            "entry_count": len(history_entries),
            "digest": _window_digest(history_entries),
            "state_hashes": [entry["state_hash"] for entry in history_entries],
        }
        predicted_objects: list[dict[str, Any]] = []
        encoded_objects = {int(item["object_id"]): item for item in encoded["objects"]}

        for object_id in range(spec.object_count):
            observation = encoded_objects.get(object_id, {"object_id": object_id, "status": "missing"})
            previous = states.get(object_id)
            status = str(observation.get("status", "missing"))
            if frame_index == 0:
                raw_box = observation.get("box")
                box: Box = (
                    tuple(float(value) for value in raw_box)
                    if raw_box is not None and len(raw_box) == 4
                    else (0.0, 0.0, 0.0, 0.0)
                )
                velocity = (0.0, 0.0)
                acceleration = (0.0, 0.0)
                confidence = 1.0 if status == "observed" else 0.0
                source = "observed" if status == "observed" else "extrapolated_missing"
                age = 0
                if status == "observed":
                    last_observed[object_id] = 0
            elif status == "observed" and observation.get("box") is not None:
                box = tuple(float(value) for value in observation["box"])
                velocity = (
                    _safe_float(observation.get("velocity", [0.0, 0.0])[0]),
                    _safe_float(observation.get("velocity", [0.0, 0.0])[1]),
                )
                acceleration = (
                    _safe_float(observation.get("acceleration", [0.0, 0.0])[0]),
                    _safe_float(observation.get("acceleration", [0.0, 0.0])[1]),
                )
                confidence = _safe_float(observation.get("extrapolation_confidence"), 1.0)
                source = "observed"
                age = 0
                last_observed[object_id] = frame_index
            else:
                if previous is None:
                    raw_box = observation.get("box")
                    box = (
                        tuple(float(value) for value in raw_box)
                        if raw_box is not None and len(raw_box) == 4
                        else (0.0, 0.0, 0.0, 0.0)
                    )
                    velocity = (0.0, 0.0)
                    acceleration = (0.0, 0.0)
                    confidence = 0.0
                else:
                    previous_box = tuple(float(value) for value in previous["box"])
                    previous_velocity = tuple(float(value) for value in previous["velocity"])
                    previous_acceleration = tuple(float(value) for value in previous["acceleration"])
                    window_velocity = _window_velocity(history_entries, object_id)
                    if window_velocity is not None and len(history_entries) > 1:
                        previous_velocity = (
                            previous_velocity[0] * 0.75 + window_velocity[0] * 0.25,
                            previous_velocity[1] * 0.75 + window_velocity[1] * 0.25,
                        )
                    damped_velocity = (
                        previous_velocity[0] * effective_decay,
                        previous_velocity[1] * effective_decay,
                    )
                    damped_acceleration = (
                        previous_acceleration[0] * effective_decay * effective_decay,
                        previous_acceleration[1] * effective_decay * effective_decay,
                    )
                    center_x, center_y = _box_center(previous_box)
                    box_width = previous_box[2] - previous_box[0]
                    box_height = previous_box[3] - previous_box[1]
                    center_x += damped_velocity[0] * dt_seconds + 0.5 * damped_acceleration[0] * dt_seconds**2
                    center_y += damped_velocity[1] * dt_seconds + 0.5 * damped_acceleration[1] * dt_seconds**2
                    box = (
                        r6(center_x - box_width / 2.0),
                        r6(center_y - box_height / 2.0),
                        r6(center_x + box_width / 2.0),
                        r6(center_y + box_height / 2.0),
                    )
                    velocity = (
                        damped_velocity[0] + damped_acceleration[0] * dt_seconds,
                        damped_velocity[1] + damped_acceleration[1] * dt_seconds,
                    )
                    acceleration = damped_acceleration
                    confidence = _safe_float(previous.get("confidence"), 0.0) * effective_decay
                source = "extrapolated_occluded" if status == "occluded" else "extrapolated_missing"
                age = frame_index - last_observed.get(object_id, 0)

            center_x, center_y = _box_center(box)
            if (
                center_x < -extrapolation_margin
                or center_x > spec.canvas_width + extrapolation_margin
                or center_y < -extrapolation_margin
                or center_y > spec.canvas_height + extrapolation_margin
            ):
                raise BusinessError(
                    E_EXTRAPOLATION_OOB,
                    f"predicted center ({r6(center_x)}, {r6(center_y)}) outside extrapolation guard",
                    spec.clip_id,
                    frame_index,
                    {
                        "object_id": object_id,
                        "center": [r6(center_x), r6(center_y)],
                        "canvas": [spec.canvas_width, spec.canvas_height],
                        "history_window": history_summary,
                    },
                    located_frames=[item["frame_index"] for item in output_frames],
                )

            state = {
                "object_id": object_id,
                "status": status,
                "source": source,
                "box": [r6(value) for value in box],
                "velocity": [r6(velocity[0]), r6(velocity[1])],
                "acceleration": [r6(acceleration[0]), r6(acceleration[1])],
                "confidence": r6(max(0.0, min(1.0, confidence))),
                "extrapolation_age": age,
            }
            states[object_id] = state
            predicted_objects.append(state)

        state_payload = {
            "clip_id": spec.clip_id,
            "frame_index": frame_index,
            "timestamp_ms": encoded["timestamp_ms"],
            "objects": predicted_objects,
            "history_digest": history_summary["digest"],
            "window_length": window_length,
            "decay": effective_decay,
        }
        state_hash = sha256_bytes(canonical_bytes(state_payload))
        history_entry = {
            "frame_index": frame_index,
            "timestamp_ms": encoded["timestamp_ms"],
            "state_hash": state_hash,
            "objects": [
                {
                    "object_id": item["object_id"],
                    "status": item["status"],
                    "box": item["box"],
                    "velocity": item["velocity"],
                }
                for item in predicted_objects
            ],
        }
        history.append(history_entry)

        frame_record = {
            "clip_id": spec.clip_id,
            "frame_index": frame_index,
            "timestamp_ms": int(encoded["timestamp_ms"]),
            "observed": bool(encoded["observed"]),
            "source_pair": encoded.get("pair_ref"),
            "history_window": history_summary,
            "state_hash": state_hash,
            "objects": predicted_objects,
            "predicted_masks": {
                str(item["object_id"]): make_predicted_mask(
                    spec,
                    int(item["object_id"]),
                    frame_index,
                    tuple(item["box"]),
                    str(item["source"]),
                )
                for item in predicted_objects
            },
        }
        output_frames.append(frame_record)

    payload: dict[str, Any] = {
        "clip_id": spec.clip_id,
        "window_length": window_length,
        "decay": effective_decay,
        "frame_indices": [frame["frame_index"] for frame in output_frames],
        "frames": output_frames,
    }
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload

