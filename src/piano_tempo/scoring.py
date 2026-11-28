"""按曲目段落汇总波动评分并映射陪练建议优先级。"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from .models import AnomalyInterval, Forecast, SectionScore

SECTIONS = [
    ("A", 0.0, 16.0),
    ("B", 16.0, 32.0),
    ("C", 32.0, 48.0),
    ("D", 48.0, 64.0),
]


def _overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def _priority(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["high"]:
        return "high"
    if score >= thresholds["medium"]:
        return "medium"
    return "low"


def score_sections(
    student_id: str,
    piece_id: str,
    anomalies: list[AnomalyInterval],
    changepoint_beats: list[float],
    forecasts: list[Forecast],
    config: dict,
) -> list[SectionScore]:
    """综合变点数量、预测区间平均宽度与异常片段占比计算段落评分。"""
    weights = config["scoring"]
    thresholds = weights["priority_thresholds"]
    scores: list[SectionScore] = []

    for section_id, start_beat, end_beat in SECTIONS:
        section_len = end_beat - start_beat
        n_changepoints = sum(
            start_beat <= beat < end_beat for beat in changepoint_beats
        )
        anomaly_coverage = 0.0
        for anomaly in anomalies:
            anomaly_coverage += _overlap(
                start_beat,
                end_beat,
                anomaly.start_beat,
                anomaly.end_beat,
            )
        anomaly_ratio = min(1.0, anomaly_coverage / section_len)

        widths: list[float] = []
        for forecast in forecasts:
            mask = (forecast.grid_beats >= start_beat) & (
                forecast.grid_beats < end_beat
            )
            if mask.any():
                widths.extend((forecast.upper - forecast.lower)[mask].tolist())
        mean_width = float(np.mean(widths)) if widths else 0.0

        score = (
            weights["weight_changepoints"] * n_changepoints
            + weights["weight_interval_width"] * mean_width
            + weights["weight_anomaly_ratio"] * anomaly_ratio
        )
        score = min(100.0, score)
        scores.append(
            SectionScore(
                student_id=student_id,
                piece_id=piece_id,
                section_id=section_id,
                start_beat=start_beat,
                end_beat=end_beat,
                n_changepoints=int(n_changepoints),
                mean_interval_width=mean_width,
                anomaly_ratio=float(anomaly_ratio),
                score=float(score),
                priority=_priority(score, thresholds),
            )
        )
    return scores


def build_coaching_plan(
    all_scores: list[SectionScore],
) -> dict[str, list[dict]]:
    """按学生分组汇总并按总分降序生成陪练分配建议。"""
    grouped: dict[str, list[SectionScore]] = defaultdict(list)
    for item in all_scores:
        grouped[item.student_id].append(item)

    plan: dict[str, list[dict]] = {}
    for student_id, items in grouped.items():
        total = sum(item.score for item in items)
        top_priority = max(
            (item.priority for item in items),
            key=lambda label: {"high": 3, "medium": 2, "low": 1}[label],
        )
        ordered = sorted(items, key=lambda item: item.score, reverse=True)
        plan[student_id] = [
            {
                "rank": rank,
                "student_id": student_id,
                "piece_id": item.piece_id,
                "section_id": item.section_id,
                "score": round(item.score, 3),
                "priority": item.priority,
                "total_student_score": round(total, 3),
                "student_priority": top_priority,
                "recommendation": _recommendation(item.priority),
            }
            for rank, item in enumerate(ordered, start=1)
        ]
    return dict(
        sorted(
            plan.items(),
            key=lambda pair: sum(entry["score"] for entry in pair[1]),
            reverse=True,
        )
    )


def _recommendation(priority: str) -> str:
    if priority == "high":
        return "优先安排固定时段陪练，重点练习突变与偏移段落"
    if priority == "medium":
        return "每周增加一次陪练并复评速度稳定性"
    return "维持常规课后练习，按周抽查"
