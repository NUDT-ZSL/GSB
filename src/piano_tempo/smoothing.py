"""逐音瞬时速度与局部加权回归（LOWESS）平滑。

瞬时速度按音符时值与相邻音间隔计算（BPM），左右手分别处理；
LOWESS 使用三立方核的局部加权线性最小二乘，在等间隔拍网格上输出逐段速度曲线。
"""
from __future__ import annotations

import numpy as np

from .models import NoteEvent, TempoCurve


def instantaneous_tempo(
    events: list[NoteEvent], base_bpm: float
) -> tuple[np.ndarray, np.ndarray]:
    """返回（逐音起始时刻拍, 瞬时速度 BPM）。

    tempo_i = 音符时值（拍） / 相邻音间隔（拍） * 60
    首音使用后向间隔，缺失或非正间隔回退到曲目基准拍速。
    """
    ordered = sorted(events, key=lambda event: event.onset_beat)
    onsets = np.asarray([event.onset_beat for event in ordered], dtype=float)
    durations = np.asarray(
        [event.duration_beats for event in ordered], dtype=float
    )
    tempos = np.full(onsets.shape, float(base_bpm))
    for idx in range(len(ordered)):
        if idx == 0:
            ioi = onsets[1] - onsets[0] if len(ordered) > 1 else None
        else:
            ioi = onsets[idx] - onsets[idx - 1]
        if ioi is not None and np.isfinite(ioi) and ioi > 1e-6:
            tempos[idx] = durations[idx] / ioi * 60.0
        else:
            tempos[idx] = float(base_bpm)
    return onsets, tempos


def lowess_smooth(
    x: np.ndarray,
    y: np.ndarray,
    grid: np.ndarray,
    bandwidth: float,
    min_points: int = 3,
) -> np.ndarray:
    """局部加权线性回归（三立方核），在给定网格上求值。

    bandwidth 为半窗带宽（拍）；窗口内点数不足 min_points 时扩大到最近邻窗口。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    result = np.empty_like(grid, dtype=float)

    for idx, center in enumerate(grid):
        distances = np.abs(x - center)
        within = distances <= bandwidth
        if within.sum() < min_points:
            nearest = np.argsort(distances)[: min(min_points, len(x))]
            within = np.zeros_like(within)
            within[nearest] = True
        local_x = x[within]
        local_y = y[within]
        effective_bandwidth = max(
            bandwidth, float(np.max(np.abs(local_x - center))) + 1e-9
        )
        weights = np.clip(
            np.abs(local_x - center) / effective_bandwidth, 0.0, 1.0
        )
        weights = (1.0 - weights**3) ** 3

        design = np.column_stack([np.ones_like(local_x), local_x - center])
        weighted = design * weights[:, None]
        try:
            beta, *_ = np.linalg.lstsq(
                weighted.T @ design, weighted.T @ local_y, rcond=None
            )
            result[idx] = beta[0]
        except np.linalg.LinAlgError:
            result[idx] = float(np.average(local_y, weights=weights))
    return result


def build_tempo_curve(
    events: list[NoteEvent],
    voice: str,
    base_bpm: float,
    grid_step_beats: float,
    bandwidth_beats: float,
    min_points: int,
) -> tuple[TempoCurve, np.ndarray, np.ndarray]:
    """对单声部构建平滑速度曲线，返回曲线对象及原始逐音（时刻, 速度）。"""
    voice_events = [event for event in events if event.voice == voice]
    note_times, note_tempos = instantaneous_tempo(voice_events, base_bpm)
    end_beat = float(note_times.max(initial=0.0))
    grid = np.arange(0.0, end_beat + grid_step_beats * 0.5, grid_step_beats)
    grid = grid[grid <= end_beat + 1e-9]
    smoothed = lowess_smooth(
        note_times, note_tempos, grid, bandwidth_beats, min_points
    )
    curve = TempoCurve(
        grid_beats=grid,
        tempo_bpm=smoothed,
        bandwidth_beats=float(bandwidth_beats),
        grid_step_beats=float(grid_step_beats),
        voice=voice,
    )
    return curve, note_times, note_tempos
