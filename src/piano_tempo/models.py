"""统一数据模型：三版本归一化后的逐音事件、速度曲线与检测结果。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np


@dataclass
class NoteEvent:
    """单个逐音事件，时刻统一为以拍为单位的浮点值。"""

    pitch: int                      # 统一后的 MIDI 音高编号
    pitch_name: str                 # 音名加八度，如 C#4
    onset_beat: float               # 起始时刻（拍）
    duration_beats: float           # 音符时值（拍）
    voice: str                      # "rh"（右手旋律）或 "lh"（左手伴奏）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TempoCurve:
    """等间隔时间网格上的速度曲线（BPM）。"""

    grid_beats: np.ndarray
    tempo_bpm: np.ndarray
    bandwidth_beats: float
    grid_step_beats: float
    voice: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_beats": self.grid_beats.tolist(),
            "tempo_bpm": self.tempo_bpm.tolist(),
            "bandwidth_beats": self.bandwidth_beats,
            "grid_step_beats": self.grid_step_beats,
            "voice": self.voice,
        }


@dataclass
class AnomalyInterval:
    """异常时间区间：速度突变或节奏偏移。"""

    start_beat: float
    end_beat: float
    direction: str          # "accel" / "ritard" / "rush" / "drag" / "pause" / "skip"
    magnitude: float        # 偏移量（BPM 或相邻音间隔相对偏差）
    kind: str               # "tempo_jump" / "rhythm_offset" / "robust_residual"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Forecast:
    """趋势项 + AR 残差组合模型的滚动预测结果。"""

    grid_beats: np.ndarray
    observed: np.ndarray
    predicted: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    interval_level: float
    empirical_coverage: float
    model_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_beats": self.grid_beats.tolist(),
            "observed": self.observed.tolist(),
            "predicted": self.predicted.tolist(),
            "lower": self.lower.tolist(),
            "upper": self.upper.tolist(),
            "interval_level": self.interval_level,
            "empirical_coverage": self.empirical_coverage,
            "model_summary": self.model_summary,
        }


@dataclass
class NormalizedRecord:
    """归一化读取器输出的统一逐音事件序列对象。"""

    student_id: str
    piece_id: str
    format_version: str
    base_bpm: float
    practice_dates: list[str]
    difficulty_sections: list[dict[str, Any]]
    events: list[NoteEvent]
    reference_curve: dict[str, np.ndarray]      # grid_beats / tempo_bpm
    true_note_tempo: dict[str, np.ndarray]      # voice -> 逐音真值 BPM
    true_sections: list[dict[str, Any]]         # 生成器内嵌异常真值
    truth_grid: np.ndarray = field(default_factory=lambda: np.array([]))
    truth_tempo: np.ndarray = field(default_factory=lambda: np.array([]))

    def events_of(self, voice: str) -> list[NoteEvent]:
        return [event for event in self.events if event.voice == voice]

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "piece_id": self.piece_id,
            "format_version": self.format_version,
            "base_bpm": self.base_bpm,
            "practice_dates": self.practice_dates,
            "difficulty_sections": self.difficulty_sections,
            "events": [event.to_dict() for event in self.events],
            "reference_curve": {
                key: value.tolist() for key, value in self.reference_curve.items()
            },
            "true_note_tempo": {
                key: value.tolist() for key, value in self.true_note_tempo.items()
            },
            "true_sections": self.true_sections,
        }


@dataclass
class SectionScore:
    """曲目段落波动评分与陪练优先级。"""

    student_id: str
    piece_id: str
    section_id: str
    start_beat: float
    end_beat: float
    n_changepoints: int
    mean_interval_width: float
    anomaly_ratio: float
    score: float
    priority: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
