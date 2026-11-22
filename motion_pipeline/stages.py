"""The four deterministic pipeline transforms."""

from __future__ import annotations

import math
from typing import Any

from .config import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    SUPPORTED_TEMPORAL_POLICIES,
    PipelineConfig,
)
from .deterministic import content_hash
from .errors import BusinessError


def _round(value: float, digits: int = 9) -> float:
    return round(float(value), digits)


def _observation_count(frame_count: int, ratio: float) -> int:
    target = int(math.floor(frame_count * ratio + 0.5))
    return max(2, min(frame_count - 1, target))


def sample_frames(dataset: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
    """Stage 1: deterministic frame-pair and observation/forecast split."""

    clips: dict[str, Any] = {}
    for clip_id in dataset["clip_ids"]:
        spec = dataset["spec_by_clip"][clip_id]
        frame_count = spec["frame_count"]
        policy = spec["temporal_policy"]
        stride = SUPPORTED_TEMPORAL_POLICIES[policy]
        observed_count = _observation_count(frame_count, config.observation_ratio)
        observed_indices = list(range(observed_count))
        forecast_indices = list(range(observed_count, frame_count))
        sampled_indices = list(range(0, observed_count, stride))
        if sampled_indices[-1] != observed_count - 1:
            sampled_indices.append(observed_count - 1)
        pairs: list[dict[str, Any]] = []
        for pair_index, (left, right) in enumerate(
            zip(sampled_indices, sampled_indices[1:])
        ):
            step_frames = right - left
            pairs.append(
                {
                    "pair_index": pair_index,
                    "from_frame_index": left,
                    "to_frame_index": right,
                    "step_frames": step_frames,
                    "step_ms": step_frames * spec["frame_interval_ms"],
                    "step_s": _round(
                        step_frames * spec["frame_interval_ms"] / 1000.0, 6
                    ),
                }
            )
        pairs.sort(
            key=lambda item: (
                item["from_frame_index"],
                item["to_frame_index"],
                item["step_frames"],
            )
        )
        record_body = {
            "clip_id": clip_id,
            "frame_count": frame_count,
            "frame_interval_ms": spec["frame_interval_ms"],
            "temporal_policy": policy,
            "stride": stride,
            "observed_indices": observed_indices,
            "forecast_indices": forecast_indices,
            "pairs": pairs,
            "tie_break": "frame_index_ascending",
            "pair_count": len(pairs),
            "observation_count": observed_count,
            "forecast_count": len(forecast_indices),
        }
        record = dict(record_body)
        record["sample_checksum"] = content_hash(record_body)
        clips[clip_id] = record
    return {
        "stage": "frame_sample",
        "config": {
            "observation_ratio": config.observation_ratio,
        },
        "clip_ids": list(dataset["clip_ids"]),
        "clips": clips,
    }


def _object_centroids(frame: dict[str, Any]) -> dict[int, tuple[float, float, int]]:
    pixel_values = {(entry[0], entry[1]): entry[2] for entry in frame["pixels"]}
    object_pixels: dict[int, list[tuple[float, float, int]]] = {}
    for row, col, object_id in frame["mask"]:
        intensity = pixel_values.get((row, col))
        if intensity is None or intensity <= 0:
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "mask support has no matching positive pixel intensity",
                field="pixels",
                detail={"row": row, "col": col},
            )
        object_pixels.setdefault(object_id, []).append(
            (float(col), float(row), int(intensity))
        )
    result: dict[int, tuple[float, float, int]] = {}
    for object_id, entries in sorted(object_pixels.items()):
        total_weight = sum(entry[2] for entry in entries)
        if total_weight == 0:
            raise BusinessError(
                "E_FRAME_DATA_INVALID",
                "object mask has zero total pixel weight",
                field="pixels",
            )
        x = sum(entry[0] * entry[2] for entry in entries) / total_weight
        y = sum(entry[1] * entry[2] for entry in entries) / total_weight
        result[object_id] = (x, y, len(entries))
    return result


def _object_mask_counts(frame: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for _row, _col, object_id in frame["mask"]:
        counts[object_id] = counts.get(object_id, 0) + 1
    return counts


def encode_motion(
    dataset: dict[str, Any], sampled: dict[str, Any], config: PipelineConfig
) -> dict[str, Any]:
    """Stage 2: estimate object displacement from sparse pixels and masks."""

    clips: dict[str, Any] = {}
    for clip_id in dataset["clip_ids"]:
        spec = dataset["spec_by_clip"][clip_id]
        raw_clip = dataset["frames_by_clip"][clip_id]
        frames = raw_clip["frames"]
        sampling = sampled["clips"][clip_id]
        pairs = sampling["pairs"]
        object_samples: dict[int, list[dict[str, Any]]] = {
            object_id: [] for object_id in range(spec["object_count"])
        }
        previous_velocity: dict[int, tuple[float, float]] = {}
        previous_end_time = 0.0
        for pair in pairs:
            left_frame = frames[pair["from_frame_index"]]
            right_frame = frames[pair["to_frame_index"]]
            left_centroids = _object_centroids(left_frame)
            right_centroids = _object_centroids(right_frame)
            left_counts = _object_mask_counts(left_frame)
            right_counts = _object_mask_counts(right_frame)
            dt_s = pair["step_s"]
            end_time_s = (
                pair["to_frame_index"] * spec["frame_interval_ms"] / 1000.0
            )
            for object_id in range(spec["object_count"]):
                if object_id not in left_centroids or object_id not in right_centroids:
                    raise BusinessError(
                        "E_FRAME_DATA_INVALID",
                        "sampled frame pair does not contain every object",
                        field="mask",
                        clip_id=clip_id,
                    )
                dx = right_centroids[object_id][0] - left_centroids[object_id][0]
                dy = right_centroids[object_id][1] - left_centroids[object_id][1]
                vx = dx / dt_s
                vy = dy / dt_s
                if object_id in previous_velocity and end_time_s > previous_end_time:
                    previous_vx, previous_vy = previous_velocity[object_id]
                    delta_t = end_time_s - previous_end_time
                    ax = (vx - previous_vx) / delta_t
                    ay = (vy - previous_vy) / delta_t
                else:
                    ax = 0.0
                    ay = 0.0
                previous_velocity[object_id] = (vx, vy)
                coverage = min(
                    1.0,
                    min(left_counts.get(object_id, 0), right_counts.get(object_id, 0))
                    / 9.0,
                )
                confidence = coverage / (1.0 + 0.08 * pair["step_frames"])
                object_samples[object_id].append(
                    {
                        "pair_index": pair["pair_index"],
                        "from_frame_index": pair["from_frame_index"],
                        "to_frame_index": pair["to_frame_index"],
                        "step_frames": pair["step_frames"],
                        "step_ms": pair["step_ms"],
                        "displacement": [_round(dx, 6), _round(dy, 6)],
                        "velocity": [_round(vx, 6), _round(vy, 6)],
                        "speed": _round(math.hypot(vx, vy), 6),
                        "acceleration": [_round(ax, 6), _round(ay, 6)],
                        "confidence": _round(confidence, 6),
                    }
                )
            previous_end_time = end_time_s
        last_observed_index = sampling["observation_count"] - 1
        last_frame = frames[last_observed_index]
        first_frame = frames[0]
        last_centroids = _object_centroids(last_frame)
        first_centroids = _object_centroids(first_frame)
        objects: dict[str, Any] = {}
        for object_id in range(spec["object_count"]):
            samples = object_samples[object_id]
            last_sample = samples[-1]
            state = last_frame["objects"][object_id]
            first_cx, first_cy, _ = first_centroids[object_id]
            last_cx, last_cy, _ = last_centroids[object_id]
            mean_speed = sum(item["speed"] for item in samples) / len(samples)
            mean_confidence = sum(item["confidence"] for item in samples) / len(samples)
            summary = {
                "sample_count": len(samples),
                "first_centroid": [_round(first_cx, 6), _round(first_cy, 6)],
                "last_centroid": [_round(last_cx, 6), _round(last_cy, 6)],
                "last_velocity": last_sample["velocity"],
                "last_acceleration": last_sample["acceleration"],
                "mean_speed": _round(mean_speed, 6),
                "mean_confidence": _round(mean_confidence, 6),
                "box_w": float(state["w"]),
                "box_h": float(state["h"]),
            }
            objects[str(object_id)] = {
                "object_id": object_id,
                "samples": samples,
                "summary": summary,
            }
        record_body = {
            "clip_id": clip_id,
            "pairs_consumed": len(pairs),
            "objects": objects,
        }
        clips[clip_id] = dict(
            record_body, feature_checksum=content_hash(record_body)
        )
    return {
        "stage": "motion_encoding",
        "clip_ids": list(dataset["clip_ids"]),
        "clips": clips,
    }


def _weighted_slope(
    entries: list[dict[str, Any]], decay: float, dt_s: float
) -> tuple[float, float, bool]:
    if len(entries) < 2:
        return 0.0, 0.0, False
    last_index = entries[-1]["frame_index"]
    times = [(entry["frame_index"] - last_index) * dt_s for entry in entries]
    weights = [decay ** (last_index - entry["frame_index"]) for entry in entries]
    total_weight = sum(weights)
    mean_t = sum(weight * time for weight, time in zip(weights, times)) / total_weight
    mean_x = (
        sum(weight * entry["cx"] for weight, entry in zip(weights, entries))
        / total_weight
    )
    mean_y = (
        sum(weight * entry["cy"] for weight, entry in zip(weights, entries))
        / total_weight
    )
    variance = sum(
        weight * (time - mean_t) ** 2 for weight, time in zip(weights, times)
    )
    if variance <= 1e-15:
        return 0.0, 0.0, False
    covariance_x = sum(
        weight * (time - mean_t) * (entry["cx"] - mean_x)
        for weight, time, entry in zip(weights, times, entries)
    )
    covariance_y = sum(
        weight * (time - mean_t) * (entry["cy"] - mean_y)
        for weight, time, entry in zip(weights, times, entries)
    )
    return covariance_x / variance, covariance_y / variance, True


def _prediction_blob(
    center_x: float, center_y: float
) -> list[list[int]]:
    cx = int(round(center_x))
    cy = int(round(center_y))
    return [
        [row, col]
        for row in range(cy - 1, cy + 2)
        for col in range(cx - 1, cx + 2)
        if 0 <= row < FRAME_HEIGHT and 0 <= col < FRAME_WIDTH
    ]


def _clamp(value: float, size: int) -> float:
    return min(size - 3.0, max(2.0, value))


def extrapolate(
    dataset: dict[str, Any],
    sampled: dict[str, Any],
    encoded: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any]:
    """Stage 3: recursive prediction with explicit rolling-history state.

    The stage starts from the final committed observation. Any intermediate
    prediction state is deliberately not persisted until all clips complete.
    """

    clips: dict[str, Any] = {}
    for clip_id in dataset["clip_ids"]:
        if (
            not isinstance(config.history_window_length, int)
            or isinstance(config.history_window_length, bool)
            or config.history_window_length < 1
            or config.history_window_length > config.max_history_window_length
        ):
            raise BusinessError(
                "E_WINDOW_INVALID",
                "history window length is outside the supported range",
                field="history_window_length",
                clip_id=clip_id,
                limit={
                    "min": 1,
                    "max": config.max_history_window_length,
                },
            )
        if (
            not math.isfinite(float(config.decay)) or float(config.decay) <= 0.0
        ):
            raise BusinessError(
                "E_DECAY_INVALID",
                "decay must be a finite positive number",
                field="decay",
                clip_id=clip_id,
            )
        spec = dataset["spec_by_clip"][clip_id]
        sampling = sampled["clips"][clip_id]
        encoding = encoded["clips"][clip_id]
        raw_frames = dataset["frames_by_clip"][clip_id]["frames"]
        window_length = config.history_window_length
        decay = float(config.decay)
        dt_s = spec["frame_interval_ms"] / 1000.0
        observation_count = sampling["observation_count"]
        forecast_indices = sampling["forecast_indices"]
        observed_window_start = max(0, observation_count - window_length)
        object_paths: dict[int, dict[str, Any]] = {}
        for object_id in range(spec["object_count"]):
            summary = encoding["objects"][str(object_id)]["summary"]
            windows: list[dict[str, Any]] = []
            for frame_index in range(observed_window_start, observation_count):
                state = raw_frames[frame_index]["objects"][object_id]
                windows.append(
                    {
                        "frame_index": frame_index,
                        "cx": _round(float(state["cx"]), 6),
                        "cy": _round(float(state["cy"]), 6),
                        "vx": _round(float(state["vx"]), 6),
                        "vy": _round(float(state["vy"]), 6),
                        "confidence": 1.0,
                        "source": "observed",
                    }
                )
            values = {
                "window": windows[-window_length:],
                "cx": _round(
                    float(
                        raw_frames[observation_count - 1]["objects"][object_id][
                            "cx"
                        ]
                    ),
                    6,
                ),
                "cy": _round(
                    float(
                        raw_frames[observation_count - 1]["objects"][object_id][
                            "cy"
                        ]
                    ),
                    6,
                ),
                "vx": float(summary["last_velocity"][0]),
                "vy": float(summary["last_velocity"][1]),
                "ax": float(summary["last_acceleration"][0]),
                "ay": float(summary["last_acceleration"][1]),
                "box_w": float(summary["box_w"]),
                "box_h": float(summary["box_h"]),
                "base_confidence": float(summary["mean_confidence"]),
            }
            path: list[dict[str, Any]] = []
            previous_consumed = observation_count - 1
            for forecast_step, frame_index in enumerate(forecast_indices):
                window = values["window"]
                input_object_hash = content_hash(
                    {
                        "consumed_frame_index": previous_consumed,
                        "window": window,
                    }
                )
                slope_x, slope_y, has_slope = _weighted_slope(window, decay, dt_s)
                vx_est = slope_x if has_slope else values["vx"]
                vy_est = slope_y if has_slope else values["vy"]
                ax_est = values["ax"]
                ay_est = values["ay"]
                next_cx = _clamp(
                    values["cx"] + vx_est * dt_s + 0.5 * ax_est * dt_s * dt_s,
                    FRAME_WIDTH,
                )
                next_cy = _clamp(
                    values["cy"] + vy_est * dt_s + 0.5 * ay_est * dt_s * dt_s,
                    FRAME_HEIGHT,
                )
                next_vx = vx_est + ax_est * dt_s
                next_vy = vy_est + ay_est * dt_s
                confidence = min(
                    1.0,
                    max(
                        0.0,
                        values["base_confidence"] * decay ** (forecast_step + 1),
                    ),
                )
                predicted_object = {
                    "object_id": object_id,
                    "cx": _round(next_cx, 6),
                    "cy": _round(next_cy, 6),
                    "w": _round(values["box_w"], 6),
                    "h": _round(values["box_h"], 6),
                    "vx": _round(next_vx, 6),
                    "vy": _round(next_vy, 6),
                    "confidence": _round(confidence, 6),
                }
                predicted_mask = [
                    [row, col, object_id]
                    for row, col in _prediction_blob(next_cx, next_cy)
                ]
                values["cx"] = _round(next_cx, 6)
                values["cy"] = _round(next_cy, 6)
                values["vx"] = _round(next_vx, 6)
                values["vy"] = _round(next_vy, 6)
                values["ax"] = _round(ax_est * decay, 6)
                values["ay"] = _round(ay_est * decay, 6)
                history_entry = {
                    "frame_index": frame_index,
                    "cx": values["cx"],
                    "cy": values["cy"],
                    "vx": values["vx"],
                    "vy": values["vy"],
                    "confidence": _round(confidence, 6),
                    "source": "predicted",
                }
                values["window"].append(history_entry)
                values["window"] = values["window"][-window_length:]
                output_object_hash = content_hash(
                    {
                        "consumed_frame_index": frame_index,
                        "window": values["window"],
                    }
                )
                path.append(
                    {
                        "frame_index": frame_index,
                        "object": predicted_object,
                        "mask": predicted_mask,
                        "confidence": confidence,
                        "previous_consumed_frame_index": previous_consumed,
                        "input_object_history_hash": input_object_hash,
                        "output_object_history_hash": output_object_hash,
                    }
                )
                previous_consumed = frame_index
            object_paths[object_id] = {
                "frames": path,
                "final_object_history_hash": (
                    path[-1]["output_object_history_hash"] if path else None
                ),
            }
        predicted_frames: list[dict[str, Any]] = []
        previous_consumed = observation_count - 1
        for forecast_step, frame_index in enumerate(forecast_indices):
            input_history_hash = content_hash(
                {
                    "consumed_frame_index": previous_consumed,
                    "object_hashes": {
                        str(object_id): object_paths[object_id]["frames"][
                            forecast_step
                        ]["input_object_history_hash"]
                        for object_id in sorted(object_paths)
                    },
                }
            )
            predicted_objects: list[dict[str, Any]] = []
            predicted_mask: list[list[int]] = []
            object_confidences: list[float] = []
            for object_id in sorted(object_paths):
                item = object_paths[object_id]["frames"][forecast_step]
                predicted_objects.append(item["object"])
                predicted_mask.extend(item["mask"])
                object_confidences.append(item["confidence"])
            predicted_mask.sort(key=lambda item: (item[0], item[1], item[2]))
            output_history_hash = content_hash(
                {
                    "consumed_frame_index": frame_index,
                    "object_hashes": {
                        str(object_id): object_paths[object_id]["frames"][
                            forecast_step
                        ]["output_object_history_hash"]
                        for object_id in sorted(object_paths)
                    },
                }
            )
            frame_confidence = (
                sum(object_confidences) / len(object_confidences)
                if object_confidences
                else 0.0
            )
            predicted_frames.append(
                {
                    "frame_index": frame_index,
                    "timestamp_ms": frame_index * spec["frame_interval_ms"],
                    "objects": predicted_objects,
                    "mask": predicted_mask,
                    "frame_confidence": _round(frame_confidence, 6),
                    "state": {
                        "input_history_summary_hash": input_history_hash,
                        "previous_consumed_frame_index": previous_consumed,
                        "output_history_summary_hash": output_history_hash,
                        "consumed_frame_index": frame_index,
                        "history_window_length": window_length,
                    },
                }
            )
            previous_consumed = frame_index
        final_state = {
            "history_summary_hash": content_hash(
                {
                    "consumed_frame_index": previous_consumed,
                    "object_hashes": {
                        str(object_id): object_paths[object_id][
                            "final_object_history_hash"
                        ]
                        for object_id in sorted(object_paths)
                    },
                }
            ),
            "consumed_frame_index": previous_consumed,
            "next_frame_index": spec["frame_count"],
            "history_window_length": window_length,
        }
        record_body = {
            "clip_id": clip_id,
            "window_length": window_length,
            "decay": decay,
            "prediction_frames": predicted_frames,
            "final_state": final_state,
        }
        clips[clip_id] = dict(
            record_body, extrapolation_checksum=content_hash(record_body)
        )
    return {
        "stage": "temporal_extrapolation",
        "config": {
            "window_length": config.history_window_length,
            "decay": float(config.decay),
        },
        "clip_ids": list(dataset["clip_ids"]),
        "clips": clips,
    }


def _box_iou(
    left: dict[str, Any], right: dict[str, Any]
) -> float:
    left_x1 = left["cx"] - left["w"] / 2.0
    left_y1 = left["cy"] - left["h"] / 2.0
    left_x2 = left["cx"] + left["w"] / 2.0
    left_y2 = left["cy"] + left["h"] / 2.0
    right_x1 = right["cx"] - right["w"] / 2.0
    right_y1 = right["cy"] - right["h"] / 2.0
    right_x2 = right["cx"] + right["w"] / 2.0
    right_y2 = right["cy"] + right["h"] / 2.0
    intersection_w = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    intersection_h = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    intersection = intersection_w * intersection_h
    union = left["w"] * left["h"] + right["w"] * right["h"] - intersection
    return intersection / union if union > 0 else 0.0


def _mask_iou(
    left_mask: list[list[int]], right_mask: list[list[int]], object_id: int
) -> float:
    left = {(row, col) for row, col, oid in left_mask if oid == object_id}
    right = {(row, col) for row, col, oid in right_mask if oid == object_id}
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _summarize_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(details)
    hit_count = sum(1 for detail in details if detail["hit"])
    if total == 0:
        return {
            "hit_count": 0,
            "total_count": 0,
            "hit_rate": 0.0,
            "mean_center_error_px": 0.0,
            "mean_box_iou": 0.0,
            "mean_mask_iou": 0.0,
            "mean_confidence": 0.0,
        }
    return {
        "hit_count": hit_count,
        "total_count": total,
        "hit_rate": _round(hit_count / total, 9),
        "mean_center_error_px": _round(
            sum(detail["center_error_px"] for detail in details) / total, 9
        ),
        "mean_box_iou": _round(
            sum(detail["box_iou"] for detail in details) / total, 9
        ),
        "mean_mask_iou": _round(
            sum(detail["mask_iou"] for detail in details) / total, 9
        ),
        "mean_confidence": _round(
            sum(detail["confidence"] for detail in details) / total, 9
        ),
    }


def aggregate(
    dataset: dict[str, Any],
    sampled: dict[str, Any],
    encoded: dict[str, Any],
    extrapolated: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any]:
    """Stage 4: prediction sequence, hit statistics, and overall metrics."""

    clips: dict[str, Any] = {}
    all_details: list[dict[str, Any]] = []
    for clip_id in dataset["clip_ids"]:
        spec = dataset["spec_by_clip"][clip_id]
        actual_frames = dataset["frames_by_clip"][clip_id]["frames"]
        predicted = extrapolated["clips"][clip_id]["prediction_frames"]
        details: list[dict[str, Any]] = []
        for frame in predicted:
            actual_frame = actual_frames[frame["frame_index"]]
            predicted_objects = {
                int(item["object_id"]): item for item in frame["objects"]
            }
            actual_objects = {
                int(item["object_id"]): item for item in actual_frame["objects"]
            }
            for object_id in range(spec["object_count"]):
                predicted_object = predicted_objects[object_id]
                actual_object = actual_objects[object_id]
                center_error = math.hypot(
                    predicted_object["cx"] - actual_object["cx"],
                    predicted_object["cy"] - actual_object["cy"],
                )
                box_iou = _box_iou(predicted_object, actual_object)
                mask_iou = _mask_iou(
                    frame["mask"], actual_frame["mask"], object_id
                )
                hit = center_error <= config.hit_distance_px
                details.append(
                    {
                        "frame_index": frame["frame_index"],
                        "object_id": object_id,
                        "hit": hit,
                        "center_error_px": _round(center_error, 9),
                        "box_iou": _round(box_iou, 9),
                        "mask_iou": _round(mask_iou, 9),
                        "confidence": predicted_object["confidence"],
                    }
                )
        summary = _summarize_details(details)
        clip_body = {
            "clip_id": clip_id,
            "prediction_frames": predicted,
            "trajectory_hits": details,
            "trajectory_stats": summary,
            "forecast_frame_count": len(predicted),
            "object_count": spec["object_count"],
        }
        clips[clip_id] = dict(
            clip_body, aggregation_checksum=content_hash(clip_body)
        )
        all_details.extend(details)
    overall = _summarize_details(all_details)
    overall["clip_count"] = len(dataset["clip_ids"])
    return {
        "stage": "result_aggregation",
        "config": {"hit_distance_px": float(config.hit_distance_px)},
        "clip_ids": list(dataset["clip_ids"]),
        "clips": clips,
        "overall_metrics": overall,
    }


def recompute_clip_metrics(per_frame_details: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute summary metrics solely from serialized per-frame details."""

    return _summarize_details(per_frame_details)


def recompute_overall_metrics(
    clip_records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    for record in clip_records.values():
        details.extend(record["trajectory_hits"])
    result = _summarize_details(details)
    result["clip_count"] = len(clip_records)
    return result
