"""以生成器内嵌真值与教师难点标注为独立依据核验处理链。"""
from __future__ import annotations

import numpy as np

from .models import AnomalyInterval, Forecast, NormalizedRecord, TempoCurve


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def curve_bias_against_truth(
    record: NormalizedRecord, curves: dict[str, TempoCurve]
) -> dict:
    """在统一拍网格上与内嵌速度真值比较，并给出逐音口径误差。"""
    per_voice = {}
    biased_segments = []
    for voice, curve in curves.items():
        grid_truth = np.interp(
            curve.grid_beats, record.truth_grid, record.truth_tempo
        )
        grid_errors = curve.tempo_bpm - grid_truth
        events = sorted(
            record.events_of(voice), key=lambda event: event.onset_beat
        )
        note_times = np.asarray([event.onset_beat for event in events])
        truth = np.interp(note_times, record.truth_grid, record.truth_tempo)
        predicted = np.interp(
            note_times, curve.grid_beats, curve.tempo_bpm
        )
        errors = predicted - truth
        per_voice[voice] = {
            "mean_bias_bpm": float(np.mean(errors)),
            "mae_bpm": float(np.mean(np.abs(errors))),
            "rmse_bpm": float(np.sqrt(np.mean(errors**2))),
            "max_abs_error_bpm": float(np.max(np.abs(errors))),
            "grid_mae_bpm": float(np.mean(np.abs(grid_errors))),
            "grid_max_abs_error_bpm": float(np.max(np.abs(grid_errors))),
        }
        step = curve.grid_step_beats
        block = max(1, int(4 / step))
        for idx in range(0, len(curve.grid_beats), block):
            end = min(len(curve.grid_beats), idx + max(1, int(4 / step)))
            segment_error = float(
                np.abs(np.mean(grid_errors[idx:end]))
            )
            biased_segments.append(
                {
                    "voice": voice,
                    "start_beat": float(curve.grid_beats[idx]),
                    "mae_bpm": round(segment_error, 4),
                }
            )
    return {"per_voice": per_voice, "segments": biased_segments}


def forecast_coverage_against_truth(
    record: NormalizedRecord, forecasts: dict[str, Forecast]
) -> dict:
    """预测区间对真实后续速度的覆盖率。"""
    result = {}
    for voice, forecast in forecasts.items():
        truth = np.interp(
            forecast.grid_beats, record.truth_grid, record.truth_tempo
        )
        inside = (truth >= forecast.lower) & (truth <= forecast.upper)
        result[voice] = {
            "nominal_level": forecast.interval_level,
            "coverage_against_truth": float(np.mean(inside)) if len(inside) else None,
            "n_points": int(len(inside)),
            "empirical_coverage_observed": forecast.empirical_coverage,
        }
    return result


def anomaly_hit_rate(
    anomalies: list[AnomalyInterval], true_sections: list[dict]
) -> dict:
    """异常片段与内嵌真值区间的命中率 / 精确率（按时间重叠判定）。"""
    hits = 0
    matched_truth = set()
    details = []
    for anomaly in anomalies:
        matched = None
        for truth_idx, truth in enumerate(true_sections):
            overlap = _overlap(
                anomaly.start_beat,
                anomaly.end_beat,
                float(truth["start_beat"]),
                float(truth["end_beat"]),
            )
            if overlap > 0:
                matched = truth_idx
                break
        if matched is not None:
            hits += 1
            matched_truth.add(matched)
        details.append(
            {
                "kind": anomaly.kind,
                "start_beat": anomaly.start_beat,
                "end_beat": anomaly.end_beat,
                "hit": matched is not None,
            }
        )
    precision = hits / len(anomalies) if anomalies else 0.0
    recall = len(matched_truth) / len(true_sections) if true_sections else 0.0
    return {
        "n_detected": len(anomalies),
        "n_truth": len(true_sections),
        "hits": hits,
        "precision": float(precision),
        "recall": float(recall),
        "details": details,
    }


def validate_run(
    record: NormalizedRecord,
    curves: dict[str, TempoCurve],
    forecasts: dict[str, Forecast],
    anomalies: list[AnomalyInterval],
    config: dict,
) -> dict:
    """汇总真值核验报告，并标记偏差超阈值片段。"""
    bias_report = curve_bias_against_truth(record, curves)
    coverage_report = forecast_coverage_against_truth(record, forecasts)
    hit_report = anomaly_hit_rate(anomalies, record.true_sections)

    threshold = config["validation"]["curve_bias_threshold_bpm"]
    coverage_tol = config["validation"]["coverage_tolerance"]
    over_threshold = [
        segment
        for segment in bias_report["segments"]
        if segment["mae_bpm"] > threshold
    ]

    coverage_summary = {}
    for voice, item in coverage_report.items():
        if item["coverage_against_truth"] is None:
            status = "insufficient_points"
        else:
            status = (
                "ok"
                if item["coverage_against_truth"]
                >= item["nominal_level"] - coverage_tol
                else "below_tolerance"
            )
        coverage_summary[voice] = {**item, "status": status}

    difficulty_hits = []
    for section in record.difficulty_sections:
        covered = [
            anomaly.kind
            for anomaly in anomalies
            if _overlap(
                anomaly.start_beat,
                anomaly.end_beat,
                float(section["start_beat"]),
                float(section["end_beat"]),
            )
            > 0
        ]
        difficulty_hits.append(
            {
                "section_id": section["section_id"],
                "label": section["label"],
                "anomaly_kinds_found": sorted(set(covered)),
                "flagged": bool(covered),
            }
        )

    return {
        "curve_bias": bias_report,
        "over_threshold_segments": over_threshold,
        "forecast_coverage": coverage_summary,
        "anomaly_hit_rate": hit_report,
        "difficulty_section_hits": difficulty_hits,
    }
