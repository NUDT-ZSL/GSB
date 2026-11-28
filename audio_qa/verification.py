"""Independent ground-truth checking for intervals, grades, and noise floors."""

from __future__ import annotations

import numpy as np


DEFAULT_TOLERANCES_MS = {
    "clipping": 80.0,
    "dc_offset": 80.0,
    "noise": 600.0,
    "impulse": 20.0,
}


def _interval_center(item: dict) -> float:
    return 0.5 * (float(item["start_sample"]) + float(item["end_sample"]))


def _overlap_samples(a: dict, b: dict) -> int:
    return max(
        0,
        min(int(a["end_sample"]), int(b["end_sample"]))
        - max(int(a["start_sample"]), int(b["start_sample"])),
    )


def match_intervals(
    truth_items: list[dict],
    detected_items: list[dict],
    tolerance_samples: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Greedy one-to-one matching by overlap, then by nearby centers."""
    used: set[int] = set()
    matches: list[dict] = []
    unmatched_truth: list[dict] = []
    for truth in sorted(truth_items, key=lambda item: int(item["start_sample"])):
        best_index = None
        best_overlap = -1
        for index, detected in enumerate(detected_items):
            if index in used:
                continue
            overlap = _overlap_samples(truth, detected)
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        if best_index is not None and best_overlap <= 0:
            best_index = None
            center = _interval_center(truth)
            near = [
                (abs(_interval_center(detected) - center), index)
                for index, detected in enumerate(detected_items)
                if index not in used and abs(_interval_center(detected) - center) <= tolerance_samples
            ]
            if near:
                best_index = min(near)[1]
                best_overlap = _overlap_samples(truth, detected_items[best_index])
        if best_index is None:
            unmatched_truth.append(truth)
            continue
        detected = detected_items[best_index]
        used.add(best_index)
        start_dev = abs(int(detected["start_sample"]) - int(truth["start_sample"]))
        end_dev = abs(int(detected["end_sample"]) - int(truth["end_sample"]))
        union = max(int(detected["end_sample"]), int(truth["end_sample"])) - min(
            int(detected["start_sample"]), int(truth["start_sample"])
        )
        matches.append(
            {
                "truth": truth,
                "detected": detected,
                "overlap_samples": int(best_overlap),
                "start_deviation_samples": int(start_dev),
                "end_deviation_samples": int(end_dev),
                "max_boundary_deviation_samples": int(max(start_dev, end_dev)),
                "iou": float(best_overlap / union) if union > 0 else 0.0,
            }
        )
    false_positives = [item for index, item in enumerate(detected_items) if index not in used]
    return matches, unmatched_truth, false_positives


def _truth_noise_on_frames(frames: list[dict], truth_noise: dict) -> np.ndarray:
    starts = np.asarray(truth_noise.get("frame_starts", []), dtype=np.float64)
    values = np.asarray(truth_noise.get("dbfs", []), dtype=np.float64)
    frame_length = float(truth_noise.get("frame_length", 1))
    if starts.size == 0 or values.size != starts.size:
        return np.full(len(frames), np.nan)
    centers = starts + 0.5 * frame_length
    output = np.full(len(frames), np.nan)
    for index, frame in enumerate(frames):
        lo, hi = float(frame["start_sample"]), float(frame["end_sample"])
        selected = values[(centers >= lo) & (centers < hi)]
        if selected.size == 0:
            nearest = int(np.argmin(np.abs(centers - 0.5 * (lo + hi))))
            selected = values[nearest : nearest + 1]
        output[index] = float(np.mean(selected))
    return output


def _frame_evidence(frames: list[dict], anchor_sample: int | None, limit: int = 5) -> list[dict]:
    if not frames:
        return []
    if anchor_sample is None:
        indices = list(range(min(limit, len(frames))))
    else:
        center = 0
        for index, frame in enumerate(frames):
            if int(frame["start_sample"]) <= anchor_sample < int(frame["end_sample"]):
                center = index
                break
        lo = max(0, center - limit // 2)
        hi = min(len(frames), lo + limit)
        indices = list(range(lo, hi))
    keys = [
        "frame_index",
        "start_sample",
        "end_sample",
        "complete",
        "peak_ratio",
        "plateau_samples",
        "near_plateau_samples",
        "dc_offset",
        "noise_floor_dbfs",
        "noise_floor_updated",
        "diff_jump_ratio",
        "impulse_flatness",
        "jump_duration_samples",
        "clipping_label",
        "impulse_candidate",
        "frame_rms_dbfs",
        "truth_noise_floor_dbfs",
        "noise_error_db",
    ]
    return [{key: frames[index].get(key) for key in keys} for index in indices]


def verify_segment(
    segment_spec: dict,
    detected: dict,
    frames: list[dict],
    sample_rate: int,
    tolerances_ms: dict[str, float] | None = None,
    noise_tolerance_db: float = 6.0,
    noise_mae_limit_db: float = 4.0,
    noise_within_fraction_min: float = 0.80,
) -> dict:
    """Compare one detected segment against the generator's independent truth."""
    truth_available = bool(segment_spec.get("truth_available", False))
    ground_truth = segment_spec.get("ground_truth")
    if not truth_available or not ground_truth:
        return {
            "status": "unverifiable",
            "reason": "生成器未为该录音段提供受损区间/噪声底真值，只报告检测结果。",
            "truth_available": False,
            "detected_interval_counts": {
                key: len(value) for key, value in detected["damage_intervals"].items()
            },
        }

    tolerances_ms = {**DEFAULT_TOLERANCES_MS, **(tolerances_ms or {})}
    truth_damages = ground_truth.get("damages", {})
    detected_intervals = detected["damage_intervals"]
    issues: list[dict] = []
    type_results: dict[str, dict] = {}
    all_matches: list[dict] = []
    first_anchor: int | None = None
    first_issue_kind: str | None = None

    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        truth_items = list(truth_damages.get(damage_type, []))
        detected_items = list(detected_intervals.get(damage_type, []))
        tolerance_samples = int(round(tolerances_ms[damage_type] * sample_rate / 1000.0))
        matches, false_negatives, false_positives = match_intervals(
            truth_items, detected_items, tolerance_samples
        )
        all_matches.extend(matches)
        boundary_failures = [
            item for item in matches if int(item["max_boundary_deviation_samples"]) > tolerance_samples
        ]
        if false_negatives:
            issues.append(
                {
                    "kind": "false_negative",
                    "type": damage_type,
                    "count": len(false_negatives),
                    "detail": "漏检真值区间",
                }
            )
            if first_anchor is None:
                first_anchor = int(false_negatives[0]["start_sample"])
                first_issue_kind = f"{damage_type}_false_negative"
        if false_positives:
            issues.append(
                {
                    "kind": "false_positive",
                    "type": damage_type,
                    "count": len(false_positives),
                    "detail": "误报检测区间",
                }
            )
            if first_anchor is None:
                first_anchor = int(false_positives[0]["start_sample"])
                first_issue_kind = f"{damage_type}_false_positive"
        if boundary_failures:
            issues.append(
                {
                    "kind": "boundary_deviation",
                    "type": damage_type,
                    "count": len(boundary_failures),
                    "tolerance_ms": float(tolerances_ms[damage_type]),
                    "max_deviation_ms": float(
                        max(item["max_boundary_deviation_samples"] for item in boundary_failures)
                    )
                    * 1000.0
                    / sample_rate,
                    "detail": "区间起止点超出段级容许偏差",
                }
            )
            if first_anchor is None:
                first_anchor = int(boundary_failures[0]["truth"]["start_sample"])
                first_issue_kind = f"{damage_type}_boundary_deviation"
        max_dev = (
            max((int(item["max_boundary_deviation_samples"]) for item in matches), default=0)
            if matches
            else None
        )
        type_results[damage_type] = {
            "truth_count": len(truth_items),
            "detected_count": len(detected_items),
            "matched_count": len(matches),
            "false_negative_count": len(false_negatives),
            "false_positive_count": len(false_positives),
            "max_boundary_deviation_samples": max_dev,
            "max_boundary_deviation_ms": (max_dev * 1000.0 / sample_rate) if max_dev is not None else None,
            "boundary_tolerance_ms": float(tolerances_ms[damage_type]),
            "matches": [
                {
                    "truth_start_sample": int(item["truth"]["start_sample"]),
                    "truth_end_sample": int(item["truth"]["end_sample"]),
                    "detected_start_sample": int(item["detected"]["start_sample"]),
                    "detected_end_sample": int(item["detected"]["end_sample"]),
                    "start_deviation_samples": int(item["start_deviation_samples"]),
                    "end_deviation_samples": int(item["end_deviation_samples"]),
                    "iou": float(item["iou"]),
                }
                for item in matches
            ],
        }

    truth_grades = ground_truth.get("segment_grades", {})
    detected_grades = detected.get("segment_grades", {})
    grade_results: dict[str, dict] = {}
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        truth_grade = truth_grades.get(damage_type)
        detected_grade = detected_grades.get(damage_type)
        match = truth_grade == detected_grade
        grade_results[damage_type] = {
            "truth_grade": truth_grade,
            "detected_grade": detected_grade,
            "match": bool(match),
        }
        if not match:
            issues.append(
                {
                    "kind": "grade_mismatch",
                    "type": damage_type,
                    "detail": f"真值分级 {truth_grade}，检测分级 {detected_grade}",
                }
            )
            if first_anchor is None:
                items = detected_intervals.get(damage_type, [])
                first_anchor = (
                    int(items[0]["start_sample"])
                    if items
                    else int(segment_spec.get("start_sample", 0))
                )
                first_issue_kind = f"{damage_type}_grade_mismatch"

    truth_overall = ground_truth.get("overall_grade")
    detected_overall = detected.get("overall_grade")
    overall_match = truth_overall == detected_overall
    if not overall_match:
        issues.append(
            {
                "kind": "overall_grade_mismatch",
                "type": "overall",
                "detail": f"真值录音段等级 {truth_overall}，检测等级 {detected_overall}",
            }
        )
        if first_anchor is None:
            for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
                items = detected_intervals.get(damage_type, [])
                if items:
                    first_anchor = int(items[0]["start_sample"])
                    first_issue_kind = "overall_grade_mismatch"
                    break
    truth_workflow = ground_truth.get("workflow", {}).get("code")
    detected_workflow = detected.get("workflow", {}).get("code")
    workflow_match = truth_workflow == detected_workflow
    if not workflow_match:
        issues.append(
            {
                "kind": "workflow_mismatch",
                "type": "workflow",
                "detail": f"真值建议 {truth_workflow}，检测建议 {detected_workflow}",
            }
        )
        if first_anchor is None:
            for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
                items = detected_intervals.get(damage_type, [])
                if items:
                    first_anchor = int(items[0]["start_sample"])
                    first_issue_kind = "workflow_mismatch"
                    break

    truth_noise = ground_truth.get("noise_floor")
    noise_result: dict
    if truth_noise:
        truth_values = _truth_noise_on_frames(frames, truth_noise)
        detected_values = np.asarray([float(f["noise_floor_dbfs"]) for f in frames], dtype=np.float64)
        updated = np.asarray([bool(f["noise_floor_updated"]) for f in frames], dtype=bool)
        complete = np.asarray([bool(f["complete"]) for f in frames], dtype=bool)
        valid = complete & updated & np.isfinite(truth_values)
        for frame, truth_value in zip(frames, truth_values):
            if np.isfinite(truth_value):
                frame["truth_noise_floor_dbfs"] = float(truth_value)
                frame["noise_error_db"] = float(frame["noise_floor_dbfs"] - truth_value)
        if valid.any():
            errors = detected_values[valid] - truth_values[valid]
            abs_errors = np.abs(errors)
            within = abs_errors <= noise_tolerance_db
            mae = float(np.mean(abs_errors))
            rmse = float(np.sqrt(np.mean(errors * errors)))
            max_error = float(np.max(abs_errors))
            within_fraction = float(np.mean(within))
            naive_values = np.asarray([float(f["frame_rms_dbfs"]) for f in frames], dtype=np.float64)
            naive_mae = float(np.mean(np.abs(naive_values[valid] - truth_values[valid])))
            mismatch_indices = np.flatnonzero(valid & (np.abs(detected_values - truth_values) > noise_tolerance_db))
            first_noise_frame = int(mismatch_indices[0]) if mismatch_indices.size else None
            if first_noise_frame is not None and first_anchor is None:
                first_anchor = int(frames[first_noise_frame]["start_sample"])
                first_issue_kind = "noise_floor_deviation"
            noise_ok = mae <= noise_mae_limit_db and within_fraction >= noise_within_fraction_min
            if not noise_ok:
                issues.append(
                    {
                        "kind": "noise_floor_deviation",
                        "type": "noise",
                        "mae_db": mae,
                        "within_tolerance_fraction": within_fraction,
                        "detail": "自适应噪声底与逐帧真值偏差超限",
                    }
                )
            elif first_issue_kind == "noise_floor_deviation":
                first_issue_kind = None
                first_anchor = None
            noise_result = {
                "status": "compared",
                "mae_db": mae,
                "naive_frame_rms_mae_db": naive_mae,
                "adaptive_improvement_db": naive_mae - mae,
                "rmse_db": rmse,
                "max_abs_error_db": max_error,
                "within_tolerance_fraction": within_fraction,
                "tolerance_db": float(noise_tolerance_db),
                "mae_limit_db": float(noise_mae_limit_db),
                "within_fraction_min": float(noise_within_fraction_min),
                "valid_frame_count": int(valid.sum()),
                "first_mismatch_frame_index": first_noise_frame,
                "first_mismatch_start_sample": (
                    int(frames[first_noise_frame]["start_sample"]) if first_noise_frame is not None else None
                ),
                "first_mismatch_truth_dbfs": (
                    float(truth_values[first_noise_frame]) if first_noise_frame is not None else None
                ),
                "first_mismatch_detected_dbfs": (
                    float(detected_values[first_noise_frame]) if first_noise_frame is not None else None
                ),
                "consistent": bool(noise_ok),
            }
        else:
            noise_result = {
                "status": "no_valid_frames",
                "consistent": False,
                "detail": "没有可比较的完整且已更新噪声底帧",
            }
            issues.append({"kind": "noise_floor_missing", "type": "noise", "detail": "无有效噪声底帧"})
    else:
        noise_result = {"status": "no_truth", "consistent": None}

    grade_order_match = all(item["match"] for item in grade_results.values()) and overall_match
    consistent = not issues
    result = {
        "status": "verified" if consistent else "inconsistent",
        "truth_available": True,
        "consistent": bool(consistent),
        "interval_results": type_results,
        "grade_results": grade_results,
        "overall_grade": {
            "truth": truth_overall,
            "detected": detected_overall,
            "match": bool(overall_match),
        },
        "workflow": {
            "truth": truth_workflow,
            "detected": detected_workflow,
            "match": bool(workflow_match),
        },
        "noise_floor": noise_result,
        "issues": issues,
        "first_issue_kind": first_issue_kind,
        "first_issue_anchor_sample": first_anchor,
        "frame_evidence": _frame_evidence(frames, first_anchor),
        "grade_order_match": bool(grade_order_match),
    }
    return result
