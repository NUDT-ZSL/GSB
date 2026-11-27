"""End-to-end underwater shot, visibility and event analysis."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from .config import DEFAULT_CONFIG, PipelineConfig
from .cuts import (
    build_shot_ranges,
    filter_turbidity_collisions,
    median_smooth,
    stabilize_cuts,
)
from .errors import PipelineError
from .features import (
    adaptive_boundary_thresholds,
    find_raw_boundary_candidates,
    frame_boundary_metrics,
)
from .segmentation import detect_turbidity_intervals, fit_piecewise_constant
from .visibility import (
    frame_visibility_series,
    score_shot,
    visibility_grade,
)


def validate_inputs(
    frames: Any,
    timestamps: Any,
) -> tuple[np.ndarray, np.ndarray]:
    frame_array = np.asarray(frames)
    timestamp_array = np.asarray(timestamps)
    if frame_array.ndim == 0 or frame_array.shape[0] == 0:
        raise PipelineError(
            "E_EMPTY_FRAMES",
            "帧序列为空，无法执行镜头切分。",
            {"frames_shape": list(frame_array.shape)},
        )
    if timestamp_array.ndim != 1:
        raise PipelineError(
            "E_INVALID_TIMESTAMPS",
            "采样时间戳必须是一维序列。",
            {"timestamps_shape": list(timestamp_array.shape)},
        )
    if timestamp_array.size == 0:
        raise PipelineError(
            "E_EMPTY_FRAMES",
            "采样时间戳为空，无法执行镜头切分。",
            {"timestamps_size": 0},
        )
    if frame_array.shape[0] != timestamp_array.size:
        raise PipelineError(
            "E_TIMESTAMP_LENGTH_MISMATCH",
            "帧数与时间戳数量不一致。",
            {
                "frame_count": int(frame_array.shape[0]),
                "timestamp_count": int(timestamp_array.size),
            },
        )
    if frame_array.ndim != 4 or frame_array.shape[3] not in (1, 3):
        raise PipelineError(
            "E_INVALID_FRAME_SHAPE",
            "帧数组必须为 (N,H,W,1) 或 (N,H,W,3)。",
            {"frames_shape": list(frame_array.shape)},
        )
    if frame_array.dtype.kind not in "iu":
        raise PipelineError(
            "E_NON_INTEGER_FRAMES",
            "帧像素必须是整数矩阵。",
            {"frames_dtype": str(frame_array.dtype)},
        )
    if frame_array.size and (
        int(frame_array.min()) < 0 or int(frame_array.max()) > 255
    ):
        raise PipelineError(
            "E_INVALID_INPUT",
            "整数帧像素值必须落在 [0, 255]。",
            {
                "min_pixel": int(frame_array.min()),
                "max_pixel": int(frame_array.max()),
            },
        )
    if not np.issubdtype(timestamp_array.dtype, np.number):
        raise PipelineError(
            "E_INVALID_TIMESTAMPS",
            "时间戳必须是数值。",
            {"timestamps_dtype": str(timestamp_array.dtype)},
        )
    timestamp_float = timestamp_array.astype(np.float64)
    if not np.all(np.isfinite(timestamp_float)):
        raise PipelineError(
            "E_INVALID_TIMESTAMPS",
            "时间戳包含 NaN 或无穷值。",
        )
    differences = np.diff(timestamp_float)
    if differences.size and np.any(differences <= 0.0):
        bad = int(np.flatnonzero(differences <= 0.0)[0])
        raise PipelineError(
            "E_TIMESTAMPS_NOT_MONOTONIC",
            "采样时间戳必须严格单调递增。",
            {
                "frame_index_before": bad,
                "frame_index_after": bad + 1,
                "timestamp_before": float(timestamp_float[bad]),
                "timestamp_after": float(timestamp_float[bad + 1]),
            },
        )
    if timestamp_float.size < 2:
        raise PipelineError(
            "E_INSUFFICIENT_FRAMES",
            "至少需要两帧才能分析镜头变化。",
            {"frame_count": int(timestamp_float.size)},
        )
    return np.ascontiguousarray(frame_array), timestamp_float


def _json_ready(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_time(timestamps: np.ndarray, frame: int) -> float:
    return float(timestamps[min(max(frame, 0), timestamps.size - 1)])


def _shot_lookup(shots: list[dict], frame: int) -> str | None:
    for shot in shots:
        if shot["start_frame"] <= frame < shot["end_frame_exclusive"]:
            return shot["shot_id"]
    return None


def _truth_comparison(
    stable_cuts: list[int],
    turbidity_intervals: list[dict],
    truth: dict | None,
) -> dict | None:
    if not truth:
        return None
    true_cuts = [int(value) for value in truth.get("true_cut_frames", [])]
    detected = sorted(int(value) for value in stable_cuts)
    missing = sorted(set(true_cuts) - set(detected))
    extra = sorted(set(detected) - set(true_cuts))
    interval_comparison: list[dict] = []
    for expected in truth.get("true_haze_intervals", []):
        expected_start = int(expected["start_frame"])
        expected_end = int(
            expected.get(
                "end_frame_exclusive",
                expected.get("end_frame", expected_start),
            )
        )
        best: dict | None = None
        for detected_interval in turbidity_intervals:
            start = int(detected_interval["start_frame"])
            end = int(detected_interval["end_frame_exclusive"])
            overlap = max(0, min(end, expected_end) - max(start, expected_start))
            union = max(end, expected_end) - min(start, expected_start)
            iou = overlap / union if union else 1.0
            if best is None or iou > best["iou"]:
                best = {
                    "expected_start_frame": expected_start,
                    "expected_end_frame_exclusive": expected_end,
                    "detected_start_frame": start,
                    "detected_end_frame_exclusive": end,
                    "overlap_frames": overlap,
                    "iou": round(float(iou), 6),
                }
        interval_comparison.append(
            best
            or {
                "expected_start_frame": expected_start,
                "expected_end_frame_exclusive": expected_end,
                "detected_start_frame": None,
                "detected_end_frame_exclusive": None,
                "overlap_frames": 0,
                "iou": 0.0,
            }
        )
    return {
        "true_cut_frames": true_cuts,
        "detected_stable_cut_frames": detected,
        "missing_cut_frames": missing,
        "extra_cut_frames": extra,
        "cut_exact_match": not missing and not extra,
        "turbidity_interval_match": interval_comparison,
    }


def analyze_frames(
    frames: Any,
    timestamps: Any,
    *,
    config: PipelineConfig = DEFAULT_CONFIG,
    source: str = "generator",
    seed: int | None = None,
    truth: dict | None = None,
) -> dict[str, Any]:
    """Run the full deterministic analysis and return a JSON-ready result."""

    frame_array, timestamp_array = validate_inputs(frames, timestamps)
    n_frames = int(frame_array.shape[0])

    metric_bundle = frame_boundary_metrics(frame_array, config)
    metrics = metric_bundle["boundary_metric"]
    thresholds = adaptive_boundary_thresholds(metrics, config)
    raw_candidates = find_raw_boundary_candidates(metrics, thresholds, config)

    frame_scores = frame_visibility_series(frame_array, config)
    smoothed_scores, turbidity_intervals = detect_turbidity_intervals(
        frame_scores, timestamp_array, config
    )

    candidates_for_stabilization, collisions = filter_turbidity_collisions(
        raw_candidates, smoothed_scores, turbidity_intervals, config
    )
    kept_boundary_frames = {
        int(item["frame_index"]) for item in candidates_for_stabilization
    }
    turbidity_filtered_frames = sorted(
        int(item["frame_index"])
        for item in raw_candidates
        if int(item["frame_index"]) not in kept_boundary_frames
    )
    for item in raw_candidates:
        if int(item["frame_index"]) in turbidity_filtered_frames:
            item["filtered_as"] = "turbidity_change"
    for item in collisions:
        item["time_s"] = round(
            _event_time(timestamp_array, int(item["frame_index"])), 6
        )
    for item in raw_candidates:
        item["time_s"] = round(
            _event_time(timestamp_array, int(item["frame_index"])), 6
        )

    stabilization = stabilize_cuts(
        raw_candidates,
        n_frames,
        timestamp_array,
        config,
        pre_removed_frames=turbidity_filtered_frames,
    )
    stable_cuts = stabilization["stable_transition_frames"]
    shots = build_shot_ranges(stable_cuts, timestamp_array)

    scored_shots: list[dict] = []
    for shot in shots:
        scored = dict(shot)
        scored.update(
            score_shot(
                frame_array,
                int(shot["start_frame"]),
                int(shot["end_frame_exclusive"]),
                config,
            )
        )
        scored_shots.append(scored)

    piecewise = fit_piecewise_constant(
        [float(item["score"]) for item in scored_shots],
        [str(item["shot_id"]) for item in scored_shots],
        config.segment_penalty,
        max_adjacent_change=config.visibility_change_points,
    )

    segment_boundaries = [
        int(item["shot_start_index"]) for item in piecewise
    ]
    frame_turbidity_changes: list[dict] = []
    for interval_index, interval in enumerate(turbidity_intervals):
        frame_turbidity_changes.append(
            {
                "frame_index": int(interval["start_frame"]),
                "time_s": float(interval["start_time_s"]),
                "direction": "influx",
                "source": "frame_visibility_interval",
                "turbidity_interval_index": interval_index,
                "visibility_jump": float(interval["visibility_drop"]),
                "event_type": "turbidity_influx",
            }
        )
        recovery_frame = int(interval["end_frame_exclusive"])
        if recovery_frame < n_frames:
            frame_turbidity_changes.append(
                {
                    "frame_index": recovery_frame,
                    "time_s": float(timestamp_array[recovery_frame]),
                    "direction": "recovery",
                    "source": "frame_visibility_interval",
                    "turbidity_interval_index": interval_index,
                    "visibility_jump": float(interval["visibility_drop"]),
                    "event_type": "turbidity_recovery",
                }
            )

    fit_change_candidates: list[dict] = []
    for index in range(1, len(piecewise)):
        previous = piecewise[index - 1]
        current = piecewise[index]
        jump = float(
            current["fitted_visibility_score"]
            - previous["fitted_visibility_score"]
        )
        if abs(jump) < config.visibility_change_points:
            continue
        shot_index = int(current["shot_start_index"])
        shot = scored_shots[shot_index]
        fit_change_candidates.append(
            {
                "frame_index": int(shot["start_frame"]),
                "time_s": float(shot["start_time_s"]),
                "direction": "influx" if jump < 0 else "recovery",
                "source": "shot_piecewise_fit",
                "turbidity_interval_index": None,
                "visibility_jump": round(jump, 6),
                "event_type": (
                    "turbidity_influx" if jump < 0 else "turbidity_recovery"
                ),
            }
        )

    combined_changes: dict[int, dict] = {}
    for point in frame_turbidity_changes:
        frame = int(point["frame_index"])
        if frame not in combined_changes:
            combined_changes[frame] = dict(point)
            combined_changes[frame]["sources"] = [point["source"]]
        else:
            existing = combined_changes[frame]
            existing["sources"] = sorted(
                set(existing["sources"] + [point["source"]])
            )
            if abs(point["visibility_jump"]) > abs(existing["visibility_jump"]):
                existing["visibility_jump"] = point["visibility_jump"]
    ordered_changes = sorted(
        combined_changes.values(), key=lambda item: item["frame_index"]
    )
    turbidity_change_points: list[dict] = []
    for point in ordered_changes:
        turbidity_change_points.append(dict(point))
    for candidate in fit_change_candidates:
        merged = False
        for point in turbidity_change_points:
            if (
                point["direction"] == candidate["direction"]
                and abs(
                    int(point["frame_index"])
                    - int(candidate["frame_index"])
                )
                <= config.turbidity_change_merge_radius
            ):
                point["sources"] = sorted(
                    set(point["sources"] + [candidate["source"]])
                )
                if abs(candidate["visibility_jump"]) > abs(
                    point["visibility_jump"]
                ):
                    point["visibility_jump"] = candidate[
                        "visibility_jump"
                    ]
                merged = True
                break
        if not merged:
            candidate = dict(candidate)
            candidate["sources"] = [candidate["source"]]
            # Keep it in the fit diagnostics below, but do not label unrelated
            # scene changes as turbidity events.
    for index, point in enumerate(turbidity_change_points):
        point["change_id"] = f"V{index + 1:02d}"
        point["sources"] = sorted(set(point["sources"]))

    events = _build_events(
        stable_cuts=stable_cuts,
        raw_candidates=raw_candidates,
        turbidity_intervals=turbidity_intervals,
        smoothed_scores=smoothed_scores,
        timestamps=timestamp_array,
        shots=scored_shots,
        config=config,
    )
    for collision in collisions:
        resolved_type = (
            collision["nearest_turbidity_event_type"]
            if collision["decision"] == "turbidity_change"
            else "shot_cut"
        )
        collision["resolved_event_type"] = resolved_type
        matching = [
            event
            for event in events
            if event["event_type"] == resolved_type
            and abs(
                int(event["frame_index"])
                - int(collision["frame_index"])
            )
            <= config.turbidity_change_merge_radius
        ]
        if matching:
            nearest_event = min(
                matching,
                key=lambda event: abs(
                    int(event["frame_index"])
                    - int(collision["frame_index"])
                ),
            )
            collision["event_table_event_id"] = nearest_event[
                "event_id"
            ]
            collision["event_table_frame_index"] = nearest_event[
                "frame_index"
            ]
            collision["event_table_frame_number"] = nearest_event[
                "frame_number"
            ]
        else:
            collision["event_table_event_id"] = None
            collision["event_table_frame_index"] = None
            collision["event_table_frame_number"] = None

    teaching_segments: list[dict] = []
    for index, fitted in enumerate(piecewise):
        selected_shots = scored_shots[
            int(fitted["shot_start_index"]) : int(
                fitted["shot_end_index_exclusive"]
            )
        ]
        start_frame = int(selected_shots[0]["start_frame"])
        end_frame = int(selected_shots[-1]["end_frame_exclusive"])
        levels = [int(shot["turbidity_level"]) for shot in selected_shots]
        level_names = [str(shot["turbidity_level_name"]) for shot in selected_shots]
        segment_events = [
            event
            for event in events
            if start_frame <= int(event["frame_index"]) < end_frame
        ]
        segment_changes = [
            point
            for point in turbidity_change_points
            if start_frame <= int(point["frame_index"]) < end_frame
        ]
        teaching_segments.append(
            {
                "segment_id": f"P{index + 1:02d}",
                "start_frame": start_frame,
                "end_frame_exclusive": end_frame,
                "start_time_s": round(float(timestamp_array[start_frame]), 6),
                "end_time_s": round(
                    float(timestamp_array[end_frame - 1]), 6
                ),
                "shot_ids": [str(shot["shot_id"]) for shot in selected_shots],
                "shot_count": len(selected_shots),
                "fitted_visibility_score": fitted["fitted_visibility_score"],
                "segment_sse": fitted["segment_sse"],
                "visibility_grade_min": min(levels),
                "visibility_grade_max": max(levels),
                "visibility_level_span_names": sorted(set(level_names)),
                "event_ids": [str(event["event_id"]) for event in segment_events],
                "event_types": sorted(
                    set(str(event["event_type"]) for event in segment_events)
                ),
                "turbidity_change_ids": [
                    str(point["change_id"]) for point in segment_changes
                ],
            }
        )

    flattened_shot_ids = [
        shot_id
        for segment in teaching_segments
        for shot_id in segment["shot_ids"]
    ]
    score_shot_ids = [item["shot_id"] for item in scored_shots]
    report_shot_ids = [item["shot_id"] for item in shots]
    consistency_checks = {
        "shot_ids_match_scores": flattened_shot_ids == score_shot_ids,
        "shot_ids_match_ranges": score_shot_ids == report_shot_ids,
        "segments_cover_every_shot_once": len(flattened_shot_ids)
        == len(set(flattened_shot_ids))
        == len(scored_shots),
        "shot_frames_are_contiguous": all(
            shots[index]["end_frame_exclusive"]
            == shots[index + 1]["start_frame"]
            for index in range(len(shots) - 1)
        )
        and shots[0]["start_frame"] == 0
        and shots[-1]["end_frame_exclusive"] == n_frames,
        "segment_frames_are_contiguous": all(
            teaching_segments[index]["end_frame_exclusive"]
            == teaching_segments[index + 1]["start_frame"]
            for index in range(len(teaching_segments) - 1)
        )
        and teaching_segments[0]["start_frame"] == 0
        and teaching_segments[-1]["end_frame_exclusive"] == n_frames,
        "event_frames_unique": len(events)
        == len(set(int(event["frame_index"]) for event in events)),
    }
    if not all(consistency_checks.values()):
        raise RuntimeError(
            f"internal consistency failure: {consistency_checks}"
        )

    frame_records = []
    for index in range(n_frames):
        frame_records.append(
            {
                "frame_index": index,
                "frame_number": index + 1,
                "time_s": round(float(timestamp_array[index]), 6),
                "hist_distance": round(
                    float(metric_bundle["hist_distance"][index]), 8
                ),
                "edge_density": round(
                    float(metric_bundle["edge_density"][index]), 8
                ),
                "edge_density_change": round(
                    float(metric_bundle["edge_density_change"][index]), 8
                ),
                "boundary_metric": round(float(metrics[index]), 8),
                "adaptive_threshold": (
                    None
                    if not np.isfinite(thresholds[index])
                    else round(float(thresholds[index]), 8)
                ),
                "visibility_score": round(float(frame_scores[index]), 6),
                "smoothed_visibility_score": round(
                    float(smoothed_scores[index]), 6
                ),
                "shot_id": _shot_lookup(shots, index),
            }
        )

    raw_map = {
        int(item["frame_index"]): item for item in raw_candidates
    }
    boundary_records = []
    for frame in stabilization["raw_transition_frames"]:
        candidate = raw_map.get(frame, {})
        boundary_records.append(
            {
                "frame_index": frame,
                "frame_number": frame + 1,
                "time_s": round(float(timestamp_array[frame]), 6),
                "hist_distance": round(
                    float(metric_bundle["hist_distance"][frame]), 8
                ),
                "edge_density_change": round(
                    float(metric_bundle["edge_density_change"][frame]), 8
                ),
                "boundary_metric": round(float(metrics[frame]), 8),
                "adaptive_threshold": round(
                    float(thresholds[frame]), 8
                ),
                "strength": round(float(candidate.get("strength", 0.0)), 6),
                "filtered_as": candidate.get("filtered_as"),
            }
        )

    frame_grade = [
        visibility_grade(float(value), config) for value in frame_scores
    ]
    frame_grade_counts = {
        name: sum(1 for _, grade_name in frame_grade if grade_name == name)
        for name in (
            "clear",
            "light_turbidity",
            "moderate_turbidity",
            "heavy_turbidity",
        )
    }

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "metadata": {
            "source": source,
            "seed": seed,
            "frame_count": n_frames,
            "frame_shape": list(frame_array.shape),
            "fps_estimated": round(
                float(1.0 / np.median(np.diff(timestamp_array))), 6
            ),
            "start_time_s": round(float(timestamp_array[0]), 6),
            "end_time_s": round(float(timestamp_array[-1]), 6),
            "config": config.to_dict(),
        },
        "frame_metrics": frame_records,
        "boundaries": {
            "raw": boundary_records,
            "raw_transition_frames": stabilization["raw_transition_frames"],
            "stable_transition_frames": stabilization["stable_transition_frames"],
            "correction_changes": stabilization["correction_changes"],
            "removals": stabilization["removals"],
            "summary": stabilization["summary"],
            "parameters": stabilization["parameters"],
            "shots": shots,
        },
        "visibility": {
            "shots": scored_shots,
            "grades": {
                "clear": {
                    "min_score": config.clear_score,
                    "max_score": 100.0,
                },
                "light_turbidity": {
                    "min_score": config.light_score,
                    "max_score": config.clear_score,
                },
                "moderate_turbidity": {
                    "min_score": config.moderate_score,
                    "max_score": config.light_score,
                },
                "heavy_turbidity": {
                    "min_score": 0.0,
                    "max_score": config.moderate_score,
                },
            },
            "frame_score_summary": frame_grade_counts,
        },
        "piecewise_fit": {
            "penalty": config.segment_penalty,
            "max_adjacent_score_change": config.visibility_change_points,
            "segments": teaching_segments,
            "breakpoint_shot_indices": segment_boundaries,
            "change_candidates": fit_change_candidates,
        },
        "teaching_segments": teaching_segments,
        "turbidity_intervals": turbidity_intervals,
        "turbidity_change_points": turbidity_change_points,
        "events": events,
        "detector_collisions": collisions,
        "consistency": {
            "checks": consistency_checks,
            "all_passed": True,
        },
        "truth_comparison": _truth_comparison(
            stable_cuts, turbidity_intervals, truth
        ),
    }
    result["content_hash"] = canonical_hash(
        {
            "frame_metrics": result["frame_metrics"],
            "boundaries": result["boundaries"],
            "visibility": result["visibility"],
            "teaching_segments": teaching_segments,
            "turbidity_intervals": turbidity_intervals,
            "turbidity_change_points": turbidity_change_points,
            "events": events,
            "detector_collisions": collisions,
        }
    )
    return _json_ready(result)


def _build_events(
    *,
    stable_cuts: list[int],
    raw_candidates: list[dict],
    turbidity_intervals: list[dict],
    smoothed_scores: np.ndarray,
    timestamps: np.ndarray,
    shots: list[dict],
    config,
) -> list[dict]:
    candidate_lookup = {
        int(item["frame_index"]): item for item in raw_candidates
    }
    event_candidates: dict[int, list[dict]] = {}

    for frame in stable_cuts:
        candidate = candidate_lookup.get(int(frame), {})
        event_candidates.setdefault(int(frame), []).append(
            {
                "event_type": "shot_cut",
                "cut_evidence": float(
                    candidate.get("boundary_metric", 0.0)
                ),
                "turbidity_interval_index": None,
            }
        )
    for interval_index, interval in enumerate(turbidity_intervals):
        start = int(interval["start_frame"])
        event_candidates.setdefault(start, []).append(
            {
                "event_type": "turbidity_influx",
                "cut_evidence": 0.0,
                "turbidity_interval_index": interval_index,
            }
        )
        end = int(interval["end_frame_exclusive"])
        if end < timestamps.size:
            event_candidates.setdefault(end, []).append(
                {
                    "event_type": "turbidity_recovery",
                    "cut_evidence": 0.0,
                    "turbidity_interval_index": interval_index,
                }
            )

    events: list[dict] = []
    for frame in sorted(event_candidates):
        candidates = event_candidates[frame]
        types = sorted(set(item["event_type"] for item in candidates))
        cut_evidence = max(
            (float(item["cut_evidence"]) for item in candidates), default=0.0
        )
        before = float(
            np.median(
                smoothed_scores[max(0, frame - 4) : max(1, frame)]
            )
        )
        after = float(
            np.median(
                smoothed_scores[
                    frame : min(smoothed_scores.size, frame + 4)
                ]
            )
        )
        visibility_jump = after - before
        visibility_evidence = abs(visibility_jump) / max(
            config.visibility_evidence_scale, 1e-9
        )
        has_cut = "shot_cut" in types
        has_turbidity = any(value.startswith("turbidity_") for value in types)
        if has_cut and has_turbidity:
            turbidity_wins = (
                visibility_evidence
                > cut_evidence * config.mixed_event_visibility_bias
            )
            if turbidity_wins:
                primary_type = next(
                    value for value in types if value.startswith("turbidity_")
                )
                decision = "visibility_score_jump"
            else:
                primary_type = "shot_cut"
                decision = "shot_boundary_metric"
        elif has_cut:
            primary_type = "shot_cut"
            decision = "only_shot_boundary_detected"
        else:
            primary_type = next(
                value for value in types if value.startswith("turbidity_")
            )
            decision = "only_visibility_change_detected"

        interval_index = next(
            (
                item["turbidity_interval_index"]
                for item in candidates
                if item["event_type"] == primary_type
            ),
            None,
        )
        events.append(
            {
                "event_id": "",
                "frame_index": int(frame),
                "frame_number": int(frame + 1),
                "time_s": round(float(timestamps[frame]), 6),
                "event_type": primary_type,
                "detected_types": types,
                "decision": decision,
                "cut_evidence": round(cut_evidence, 6),
                "visibility_jump": round(visibility_jump, 6),
                "visibility_evidence": round(visibility_evidence, 6),
                "shot_id_after": _shot_lookup(shots, frame),
                "turbidity_interval_index": interval_index,
            }
        )
    for index, event in enumerate(events):
        event["event_id"] = f"E{index + 1:02d}"
    return events
