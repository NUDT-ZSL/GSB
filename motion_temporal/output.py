"""Final per-frame prediction and motion-mask materialization."""

from __future__ import annotations

from typing import Any, Callable

from .canonical import canonical_bytes, r6, sha256_bytes
from .codes import E_INCONSISTENT, E_MASK_SHAPE
from .errors import BusinessError
from .models import ClipSpec


def build_output(
    spec: ClipSpec,
    aggregation_payload: dict[str, Any],
    sampling_payload: dict[str, Any],
    *,
    frame_callback: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    aggregate_frames = {int(frame["frame_index"]): frame for frame in aggregation_payload.get("frames", [])}
    expected = set(range(spec.frame_count))
    if set(aggregate_frames) != expected:
        missing = sorted(expected - set(aggregate_frames))
        raise BusinessError(
            E_INCONSISTENT,
            "aggregation frame set changed before output",
            spec.clip_id,
            missing[0] if missing else spec.frame_count - 1,
            {"missing_frames": missing},
        )
    pair_by_destination = {
        int(pair["dst_frame_index"]): pair for pair in sampling_payload.get("frame_pairs", [])
    }
    frames: list[dict[str, Any]] = []
    for frame_index in range(spec.frame_count):
        if frame_callback is not None:
            frame_callback(frame_index)
        aggregate = aggregate_frames[frame_index]
        predictions: list[dict[str, Any]] = []
        motion_masks: list[dict[str, Any]] = []
        for item in aggregate.get("objects", []):
            object_id = int(item["object_id"])
            predictions.append(
                {
                    "object_id": object_id,
                    "box": [r6(value) for value in item["box"]],
                    "occluded": item["status"] == "occluded",
                    "source": item["source"],
                    "confidence": r6(item["confidence"]),
                    "extrapolation_age": int(item["extrapolation_age"]),
                }
            )
            mask = aggregate.get("predicted_masks", {}).get(str(object_id))
            if mask is None:
                raise BusinessError(
                    E_MASK_SHAPE,
                    f"missing predicted mask for object {object_id}",
                    spec.clip_id,
                    frame_index,
                    {"object_id": object_id},
                )
            shape = list(mask.get("shape", []))
            if shape != [spec.canvas_height, spec.canvas_width]:
                raise BusinessError(
                    E_MASK_SHAPE,
                    f"predicted mask shape {shape} != canvas",
                    spec.clip_id,
                    frame_index,
                    {
                        "object_id": object_id,
                        "actual_shape": shape,
                        "expected_shape": [spec.canvas_height, spec.canvas_width],
                    },
                )
            for y, x in mask.get("coords", []):
                if not (0 <= int(y) < spec.canvas_height and 0 <= int(x) < spec.canvas_width):
                    raise BusinessError(
                        E_MASK_SHAPE,
                        f"predicted mask coordinate ({y}, {x}) outside canvas",
                        spec.clip_id,
                        frame_index,
                        {"object_id": object_id, "coordinate": [y, x]},
                    )
            motion_masks.append({"object_id": object_id, **mask})
        pair = pair_by_destination.get(frame_index)
        frames.append(
            {
                "clip_id": spec.clip_id,
                "frame_index": frame_index,
                "timestamp_ms": int(aggregate["timestamp_ms"]),
                "observed": bool(aggregate["observed"]),
                "predictions": predictions,
                "motion_masks": motion_masks,
                "history_window": aggregate["history_window"],
                "state_hash": aggregate["state_hash"],
                "provenance": {
                    "state_hash": aggregate["state_hash"],
                    "history_digest": aggregate["history_window"]["digest"],
                    "history_frames": aggregate["history_window"]["state_hashes"],
                    "source_pair": aggregate.get("source_pair"),
                    "sampling_pair": (
                        {
                            "pair_id": pair["pair_id"],
                            "src_frame_index": int(pair["src_frame_index"]),
                            "dst_frame_index": int(pair["dst_frame_index"]),
                        }
                        if pair is not None
                        else None
                    ),
                },
            }
        )
    payload: dict[str, Any] = {
        "clip_id": spec.clip_id,
        "frame_indices": [frame["frame_index"] for frame in frames],
        "frames": frames,
    }
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload


def drop_output_frame(output_payload: dict[str, Any], frame_index: int) -> dict[str, Any]:
    frames = [frame for frame in output_payload.get("frames", []) if int(frame["frame_index"]) != frame_index]
    payload = dict(output_payload)
    payload["frames"] = frames
    payload["frame_indices"] = [int(frame["frame_index"]) for frame in frames]
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload

