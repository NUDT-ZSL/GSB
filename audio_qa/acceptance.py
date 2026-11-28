"""Aggregate acceptance checks over verification and dual-frame comparison."""

from __future__ import annotations

import numpy as np


DAMAGE_TYPES = ("clipping", "dc_offset", "noise", "impulse")


def build_acceptance(primary: dict, comparison: dict, dual: dict) -> dict:
    type_stats: dict[str, dict] = {}
    for damage_type in DAMAGE_TYPES:
        stats = {
            "truth_count": 0,
            "detected_count": 0,
            "matched_count": 0,
            "false_negative_count": 0,
            "false_positive_count": 0,
            "grade_comparisons": 0,
            "grade_matches": 0,
            "max_boundary_deviation_ms": 0.0,
            "unverifiable_detected_count": 0,
        }
        for recording in primary["recordings"]:
            for segment in recording["segments"]:
                verification = segment["verification"]
                if verification["status"] == "unverifiable":
                    stats["unverifiable_detected_count"] += len(
                        segment["damage_intervals"][damage_type]
                    )
                    continue
                item = verification["interval_results"][damage_type]
                for key in (
                    "truth_count",
                    "detected_count",
                    "matched_count",
                    "false_negative_count",
                    "false_positive_count",
                ):
                    stats[key] += int(item.get(key, 0))
                stats["max_boundary_deviation_ms"] = max(
                    stats["max_boundary_deviation_ms"],
                    float(item.get("max_boundary_deviation_ms") or 0.0),
                )
                grade_item = verification["grade_results"][damage_type]
                stats["grade_comparisons"] += 1
                stats["grade_matches"] += int(bool(grade_item["match"]))
        type_stats[damage_type] = stats

    verified_segments = primary["summary"]["verified_segments"]
    inconsistent_segments = primary["summary"]["inconsistent_segments"]
    truth_available = primary["summary"]["truth_available_segments"]
    unverifiable = primary["summary"]["unverifiable_segments"]

    noise_items = []
    for analysis in (primary, comparison):
        for recording in analysis["recordings"]:
            for segment in recording["segments"]:
                verification = segment["verification"]
                if verification["status"] == "unverifiable":
                    continue
                noise = verification.get("noise_floor", {})
                if noise.get("status") == "compared":
                    noise_items.append(
                        {
                            "segment_id": segment["segment_id"],
                            "mae_db": float(noise["mae_db"]),
                            "within": float(noise["within_tolerance_fraction"]),
                            "naive_mae_db": float(noise.get("naive_frame_rms_mae_db", float("nan"))),
                            "improvement_db": float(noise.get("adaptive_improvement_db", float("nan"))),
                        }
                    )
    noise_mae_max = max((item["mae_db"] for item in noise_items), default=float("nan"))
    noise_within_min = min((item["within"] for item in noise_items), default=float("nan"))
    noise_improvement_median = (
        float(np.nanmedian([item["improvement_db"] for item in noise_items]))
        if noise_items
        else float("nan")
    )

    no_fp_fn = all(
        stats["false_negative_count"] == 0 and stats["false_positive_count"] == 0
        for stats in type_stats.values()
    )
    grade_ok = all(
        stats["grade_comparisons"] == stats["grade_matches"] for stats in type_stats.values()
    )
    workflow_ok = all(
        segment["verification"].get("workflow", {}).get("match", True)
        for recording in primary["recordings"]
        for segment in recording["segments"]
        if segment["verification"]["status"] != "unverifiable"
    )
    incomplete_marked = True
    for recording in primary["recordings"]:
        for segment in recording["segments"]:
            frames = segment["frames"]
            incomplete_positions = [index for index, frame in enumerate(frames) if not frame["complete"]]
            if not incomplete_positions:
                continue
            first = incomplete_positions[0]
            # All partial frames must form one contiguous suffix ending at the
            # segment boundary with their actual remaining sample count.
            if incomplete_positions != list(range(first, len(frames))):
                incomplete_marked = False
            if any(
                int(frame["end_sample"]) != int(segment["end_sample"])
                for frame in frames[first:]
            ):
                incomplete_marked = False
    segment_bounded = all(
        all(
            segment["start_sample"] <= frame["start_sample"] < frame["end_sample"] <= segment["end_sample"]
            for frame in segment["frames"]
        )
        for recording in primary["recordings"]
        for segment in recording["segments"]
    )
    unverifiable_segments = [
        segment
        for recording in primary["recordings"]
        for segment in recording["segments"]
        if segment["verification"]["status"] == "unverifiable"
    ]
    unverifiable_reported = (
        True
        if not unverifiable_segments
        else all(
            bool(segment["verification"].get("detected_interval_counts"))
            or sum(len(v) for v in segment["damage_intervals"].values()) == 0
            for segment in unverifiable_segments
        )
    )

    checks = [
        {
            "check": "truth_segment_verification",
            "pass": inconsistent_segments == 0 and truth_available > 0,
            "detail": (
                f"真值可用段 {truth_available}，核验通过 {verified_segments}，"
                f"不一致 {inconsistent_segments}，无法核对 {unverifiable}"
            ),
        },
        {
            "check": "interval_false_positive_false_negative",
            "pass": bool(no_fp_fn),
            "detail": "所有真值可用段的四类受损区间误报与漏报计数",
        },
        {
            "check": "segment_grade_consistency",
            "pass": bool(grade_ok),
            "detail": "四类受损逐段分级与真值一致",
        },
        {
            "check": "workflow_consistency",
            "pass": bool(workflow_ok),
            "detail": "建议流程与真值分级映射一致",
        },
        {
            "check": "adaptive_noise_floor",
            "pass": bool(
                np.isfinite(noise_mae_max)
                and noise_mae_max <= 4.0
                and noise_within_min >= 0.80
            ),
            "detail": (
                f"两套帧参数逐帧噪声底最大 MAE {noise_mae_max:.3f} dB，"
                f"最低 6 dB 内比例 {noise_within_min:.3f}，"
                f"相对全帧 RMS 的中位改善 {noise_improvement_median:.3f} dB"
            ),
        },
        {
            "check": "dual_frame_stability",
            "pass": bool(dual["stable"]),
            "detail": (
                f"稳定段 {dual['stable_segment_count']}/{dual['segment_count']}，"
                f"需复核 {dual['unstable_segment_count']} 段"
            ),
        },
        {
            "check": "incomplete_frame_marking",
            "pass": bool(incomplete_marked),
            "detail": "只有段末实际样本不足的帧被标记为不完整帧",
        },
        {
            "check": "no_cross_segment_framing",
            "pass": bool(segment_bounded),
            "detail": "所有帧均落在所属录音段内，未跨段拼接",
        },
        {
            "check": "truth_missing_segment",
            "pass": bool(unverifiable_reported),
            "detail": (
                "清单无真值缺失段"
                if not unverifiable_segments
                else f"{len(unverifiable_segments)} 个真值缺失段保留检测结果并标记无法核对"
            ),
        },
    ]
    return {
        "pass": all(item["pass"] for item in checks),
        "checks": checks,
        "type_stats": type_stats,
        "noise_floor": {
            "max_mae_db": noise_mae_max,
            "min_within_tolerance_fraction": noise_within_min,
            "median_improvement_over_frame_rms_db": noise_improvement_median,
            "compared_segment_runs": len(noise_items),
        },
        "dual_frame_stable": bool(dual["stable"]),
    }
