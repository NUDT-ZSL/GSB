"""三个格式版本处理结果的跨版本一致性比对。"""
from __future__ import annotations

import numpy as np


def _match_interval(interval, candidates, time_tol: float):
    """在候选区间中查找起止时刻最接近的同类型区间。"""
    best = None
    best_distance = float("inf")
    for candidate in candidates:
        if candidate["kind"] != interval["kind"]:
            continue
        distance = abs(candidate["start_beat"] - interval["start_beat"]) + abs(
            candidate["end_beat"] - interval["end_beat"]
        )
        if distance < best_distance:
            best_distance = distance
            best = candidate
    if best is not None and best_distance <= 2 * time_tol:
        return best, best_distance
    return None, best_distance


def compare_versions(run_results: dict[str, dict], tolerance: dict) -> dict:
    """比对逐段速度曲线、预测区间与异常片段定位的跨版本差异。"""
    versions = sorted(run_results)
    baseline = versions[0]
    curve_tol = tolerance["curve_max_deviation_bpm"]
    interval_tol = tolerance["interval_time_deviation_beats"]
    location_tol = tolerance["location_time_deviation_beats"]

    curve_differences = []
    interval_differences = []
    anomaly_differences = []
    manual_review: list[dict] = []

    for version in versions[1:]:
        for voice in ("rh", "lh"):
            base_curve = run_results[baseline]["curves"][voice]
            other_curve = run_results[version]["curves"][voice]
            common = np.minimum(len(base_curve["grid_beats"]), len(other_curve["grid_beats"]))
            max_dev = float(
                np.max(
                    np.abs(
                        np.asarray(base_curve["tempo_bpm"][:common])
                        - np.asarray(other_curve["tempo_bpm"][:common])
                    )
                )
            )
            entry = {
                "baseline": baseline,
                "version": version,
                "voice": voice,
                "max_curve_deviation_bpm": round(max_dev, 6),
                "within_tolerance": max_dev <= curve_tol,
            }
            curve_differences.append(entry)
            if not entry["within_tolerance"]:
                manual_review.append(
                    {"reason": "curve_deviation", **entry}
                )

        for voice in ("rh", "lh"):
            base_forecast = run_results[baseline]["forecasts"][voice]
            other_forecast = run_results[version]["forecasts"][voice]
            base_grid = np.asarray(base_forecast["grid_beats"])
            other_grid = np.asarray(other_forecast["grid_beats"])
            common = min(len(base_grid), len(other_grid))
            if common:
                start_dev = float(
                    abs(base_grid[:common][0] - other_grid[:common][0])
                )
                end_dev = float(
                    abs(base_grid[:common][-1] - other_grid[:common][-1])
                )
            else:
                start_dev = end_dev = float("inf")
            entry = {
                "baseline": baseline,
                "version": version,
                "voice": voice,
                "interval_start_deviation_beats": round(start_dev, 6),
                "interval_end_deviation_beats": round(end_dev, 6),
                "within_tolerance": (
                    start_dev <= interval_tol and end_dev <= interval_tol
                ),
            }
            interval_differences.append(entry)
            if not entry["within_tolerance"]:
                manual_review.append(
                    {"reason": "forecast_interval_time", **entry}
                )

        base_anomalies = run_results[baseline]["anomalies"]
        other_anomalies = run_results[version]["anomalies"]
        for anomaly in base_anomalies:
            matched, distance = _match_interval(
                anomaly, other_anomalies, location_tol
            )
            if matched is None:
                entry = {
                    "baseline": baseline,
                    "version": version,
                    "kind": anomaly["kind"],
                    "baseline_start_beat": anomaly["start_beat"],
                    "baseline_end_beat": anomaly["end_beat"],
                    "location_deviation_beats": round(distance, 6),
                    "within_tolerance": False,
                    "detail": "基线版本异常未在对比版本中匹配",
                }
            else:
                entry = {
                    "baseline": baseline,
                    "version": version,
                    "kind": anomaly["kind"],
                    "baseline_start_beat": anomaly["start_beat"],
                    "baseline_end_beat": anomaly["end_beat"],
                    "matched_start_beat": matched["start_beat"],
                    "matched_end_beat": matched["end_beat"],
                    "location_deviation_beats": round(distance, 6),
                    "within_tolerance": distance <= location_tol,
                }
            anomaly_differences.append(entry)
            if not entry["within_tolerance"]:
                manual_review.append(
                    {"reason": "anomaly_location", **entry}
                )

    return {
        "baseline_version": baseline,
        "versions": versions,
        "curve_differences": curve_differences,
        "interval_differences": interval_differences,
        "anomaly_differences": anomaly_differences,
        "manual_review": manual_review,
        "all_within_tolerance": len(manual_review) == 0,
    }
