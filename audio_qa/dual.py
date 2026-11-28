"""Compare two legal frame-parameter runs without silently preferring one."""

from __future__ import annotations

import numpy as np

from .verification import DEFAULT_TOLERANCES_MS, match_intervals


def _by_segment(analysis: dict) -> dict[str, tuple[dict, dict]]:
    result: dict[str, tuple[dict, dict]] = {}
    for recording in analysis["recordings"]:
        for segment in recording["segments"]:
            result[segment["segment_id"]] = (recording, segment)
    return result


def compare_runs(primary: dict, comparison: dict, tolerances_ms: dict[str, float] | None = None) -> dict:
    tolerances_ms = {**DEFAULT_TOLERANCES_MS, **(tolerances_ms or {})}
    sample_rate = int(primary["parameters"]["sample_rate"] or 16000)
    primary_segments = _by_segment(primary)
    comparison_segments = _by_segment(comparison)
    all_ids = list(primary_segments.keys())
    for segment_id in comparison_segments:
        if segment_id not in primary_segments:
            all_ids.append(segment_id)

    comparisons: list[dict] = []
    for segment_id in all_ids:
        if segment_id not in primary_segments or segment_id not in comparison_segments:
            comparisons.append(
                {
                    "segment_id": segment_id,
                    "stable": False,
                    "differences": [{"kind": "segment_missing_in_one_run"}],
                }
            )
            continue
        _, seg_a = primary_segments[segment_id]
        _, seg_b = comparison_segments[segment_id]
        differences: list[dict] = []
        type_results: dict[str, dict] = {}

        for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
            items_a = list(seg_a["damage_intervals"].get(damage_type, []))
            items_b = list(seg_b["damage_intervals"].get(damage_type, []))
            tolerance_samples = int(round(tolerances_ms[damage_type] * sample_rate / 1000.0))
            matches, false_negatives, false_positives = match_intervals(
                items_a, items_b, tolerance_samples
            )
            max_start_dev = max(
                (int(item["start_deviation_samples"]) for item in matches), default=0
            )
            max_end_dev = max(
                (int(item["end_deviation_samples"]) for item in matches), default=0
            )
            boundary_ok = (
                max_start_dev <= tolerance_samples and max_end_dev <= tolerance_samples
            )
            count_ok = len(items_a) == len(items_b)
            grade_a = seg_a["segment_grades"].get(damage_type)
            grade_b = seg_b["segment_grades"].get(damage_type)
            grade_ok = grade_a == grade_b
            type_results[damage_type] = {
                "count_primary": len(items_a),
                "count_comparison": len(items_b),
                "matched_count": len(matches),
                "false_negative_count": len(false_negatives),
                "false_positive_count": len(false_positives),
                "max_start_deviation_ms": max_start_dev * 1000.0 / sample_rate,
                "max_end_deviation_ms": max_end_dev * 1000.0 / sample_rate,
                "tolerance_ms": float(tolerances_ms[damage_type]),
                "boundary_ok": bool(boundary_ok),
                "count_ok": bool(count_ok),
                "grade_primary": grade_a,
                "grade_comparison": grade_b,
                "grade_ok": bool(grade_ok),
            }
            if not count_ok or false_negatives or false_positives:
                differences.append(
                    {
                        "kind": "interval_count_or_matching",
                        "type": damage_type,
                        "count_primary": len(items_a),
                        "count_comparison": len(items_b),
                        "false_negative_count": len(false_negatives),
                        "false_positive_count": len(false_positives),
                    }
                )
            if matches and not boundary_ok:
                differences.append(
                    {
                        "kind": "boundary_deviation",
                        "type": damage_type,
                        "max_start_deviation_ms": max_start_dev * 1000.0 / sample_rate,
                        "max_end_deviation_ms": max_end_dev * 1000.0 / sample_rate,
                        "tolerance_ms": float(tolerances_ms[damage_type]),
                    }
                )
            if not grade_ok:
                differences.append(
                    {
                        "kind": "grade_mismatch",
                        "type": damage_type,
                        "primary": grade_a,
                        "comparison": grade_b,
                    }
                )

        overall_a = seg_a["overall_grade"]
        overall_b = seg_b["overall_grade"]
        workflow_a = seg_a["workflow"]["code"]
        workflow_b = seg_b["workflow"]["code"]
        if overall_a != overall_b:
            differences.append(
                {
                    "kind": "overall_grade_mismatch",
                    "primary": overall_a,
                    "comparison": overall_b,
                }
            )
        if workflow_a != workflow_b:
            differences.append(
                {
                    "kind": "workflow_mismatch",
                    "primary": workflow_a,
                    "comparison": workflow_b,
                }
            )

        noise_a = np.asarray([float(f["noise_floor_dbfs"]) for f in seg_a["frames"]], dtype=np.float64)
        noise_b = np.asarray([float(f["noise_floor_dbfs"]) for f in seg_b["frames"]], dtype=np.float64)
        median_noise_a = float(np.median(noise_a)) if noise_a.size else None
        median_noise_b = float(np.median(noise_b)) if noise_b.size else None
        noise_median_delta = (
            abs(median_noise_a - median_noise_b)
            if median_noise_a is not None and median_noise_b is not None
            else None
        )
        comparisons.append(
            {
                "segment_id": segment_id,
                "stable": not differences,
                "overall_grade_primary": overall_a,
                "overall_grade_comparison": overall_b,
                "workflow_primary": workflow_a,
                "workflow_comparison": workflow_b,
                "noise_floor_median_primary_dbfs": median_noise_a,
                "noise_floor_median_comparison_dbfs": median_noise_b,
                "noise_floor_median_delta_db": noise_median_delta,
                "types": type_results,
                "differences": differences,
            }
        )

    unstable = [item for item in comparisons if not item["stable"]]
    max_dev_by_type: dict[str, float] = {}
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        values = [
            max(
                float(item["types"][damage_type]["max_start_deviation_ms"]),
                float(item["types"][damage_type]["max_end_deviation_ms"]),
            )
            for item in comparisons
            if "types" in item
        ]
        max_dev_by_type[damage_type] = max(values, default=0.0)
    return {
        "stable": not unstable,
        "tolerances_ms": tolerances_ms,
        "segment_count": len(comparisons),
        "stable_segment_count": len(comparisons) - len(unstable),
        "unstable_segment_count": len(unstable),
        "max_boundary_deviation_ms_by_type": max_dev_by_type,
        "review_segment_ids": [item["segment_id"] for item in unstable],
        "segments": comparisons,
        "note": (
            "两套帧参数均完整保留；任一不一致段均列入人工复核，不以单套结果覆盖另一套。"
            if unstable
            else "两套帧参数在段级容许偏差内一致；两套原始结果仍完整保留。"
        ),
    }
