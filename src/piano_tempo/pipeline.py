"""端到端处理链：归一化 -> 平滑 -> 检测 -> 预测 -> 稳健异常 -> 评分。"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np

from .detection import (
    detect_rhythm_offsets,
    detect_tempo_jumps,
    rolling_stationarity,
)
from .forecast import forecast_curve
from .models import AnomalyInterval, NormalizedRecord
from .normalizer import load_record, normalize_record
from .robust import detect_robust_anomalies
from .scoring import build_coaching_plan, score_sections
from .smoothing import build_tempo_curve
from .validation import validate_run


def _anomaly_to_dict(anomaly: AnomalyInterval) -> dict:
    return anomaly.to_dict()


def run_pipeline(record: NormalizedRecord, config: dict) -> dict:
    """对单条归一化记录运行完整处理链。"""
    grid_step = float(config["grid_step_beats"])
    smoothing_cfg = config["smoothing"]
    detection_cfg = config["detection"]
    forecast_cfg = config["forecast"]
    robust_cfg = config["robust"]

    curves = {}
    forecasts = {}
    all_anomalies: list[AnomalyInterval] = []
    changepoint_beats: list[float] = []
    stationarity: dict[str, list] = {}

    for voice in ("rh", "lh"):
        curve, _, _ = build_tempo_curve(
            record.events,
            voice,
            record.base_bpm,
            grid_step,
            float(smoothing_cfg["lowess_bandwidth_beats"]),
            int(smoothing_cfg["min_points"]),
        )
        curves[voice] = curve

        jumps = detect_tempo_jumps(
            curve,
            jump_bpm=float(detection_cfg["tempo_jump_bpm"]),
            k=float(detection_cfg["cusum_k"]),
            h=float(detection_cfg["cusum_h"]),
            baseline_window=int(detection_cfg["cusum_baseline_window"]),
        )
        offsets = detect_rhythm_offsets(
            record.events,
            voice,
            record.reference_curve,
            offset_ratio=float(detection_cfg["rhythm_offset_ratio"]),
        )
        stationary_flags = rolling_stationarity(
            curve,
            window=int(detection_cfg["stationarity_window"]),
            pvalue=float(detection_cfg["stationarity_pvalue"]),
        )
        robust_anomalies, _ = detect_robust_anomalies(
            curve,
            n_breakpoints=int(forecast_cfg["n_breakpoints"]),
            ar_order=int(forecast_cfg["ar_order"]),
            min_train_points=int(forecast_cfg["min_train_points"]),
            z_threshold=float(robust_cfg["z_threshold"]),
            merge_gap_beats=float(robust_cfg["merge_gap_beats"]),
        )
        forecast = forecast_curve(
            curve,
            n_breakpoints=int(forecast_cfg["n_breakpoints"]),
            ar_order=int(forecast_cfg["ar_order"]),
            interval_level=float(forecast_cfg["interval_level"]),
            horizon=int(forecast_cfg["horizon"]),
            min_train_points=int(forecast_cfg["min_train_points"]),
            interval_inflation=float(
                forecast_cfg.get("interval_inflation", 1.0)
            ),
        )
        forecasts[voice] = forecast
        stationarity[voice] = [item.to_dict() for item in stationary_flags]

        voice_anomalies = jumps + offsets + robust_anomalies
        for anomaly in voice_anomalies:
            anomaly.detail["voice"] = voice
        all_anomalies.extend(voice_anomalies)
        for jump in jumps:
            changepoint_beats.append(
                float(jump.detail.get("beat", jump.start_beat))
            )

    section_scores = score_sections(
        record.student_id,
        record.piece_id,
        all_anomalies,
        changepoint_beats,
        list(forecasts.values()),
        config,
    )
    validation = validate_run(
        record, curves, forecasts, all_anomalies, config
    )

    return {
        "record_id": f"{record.student_id}_{record.piece_id}_{record.format_version}",
        "student_id": record.student_id,
        "piece_id": record.piece_id,
        "format_version": record.format_version,
        "practice_dates": record.practice_dates,
        "curves": {
            voice: curve.to_dict() for voice, curve in curves.items()
        },
        "forecasts": {
            voice: forecast.to_dict()
            for voice, forecast in forecasts.items()
        },
        "stationarity_flags": stationarity,
        "anomalies": [_anomaly_to_dict(item) for item in all_anomalies],
        "section_scores": [asdict(item) for item in section_scores],
        "validation": validation,
        "config_echo": {
            "grid_step_beats": grid_step,
            "bandwidth_beats": float(smoothing_cfg["lowess_bandwidth_beats"]),
            "interval_level": float(forecast_cfg["interval_level"]),
        },
    }


def run_from_file(path: str | Path, config: dict) -> dict:
    record = load_record(path, float(config["grid_step_beats"]))
    return run_pipeline(record, config)


def run_from_payload(payload: dict, config: dict) -> dict:
    record = normalize_record(payload, float(config["grid_step_beats"]))
    return run_pipeline(record, config)


def aggregate_coaching(run_results: list[dict]) -> dict:
    """汇总全部记录的段落评分，生成按学生分组的陪练建议。"""
    from .models import SectionScore

    # 同一学生/曲目/段落的三个格式版本取均值，避免版本重复计入评分
    buckets: dict[tuple, list[dict]] = {}
    for result in run_results:
        for item in result["section_scores"]:
            key = (
                item["student_id"],
                item["piece_id"],
                item["section_id"],
            )
            buckets.setdefault(key, []).append(item)
    scores = []
    for items in buckets.values():
        averaged = dict(items[0])
        for field_name in (
            "n_changepoints",
            "mean_interval_width",
            "anomaly_ratio",
            "score",
        ):
            averaged[field_name] = float(
                np.mean([item[field_name] for item in items])
            )
        averaged["n_changepoints"] = int(round(averaged["n_changepoints"]))
        scores.append(SectionScore(**averaged))
    return build_coaching_plan(scores)
