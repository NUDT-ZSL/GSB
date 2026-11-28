"""滚动窗口平稳性检验、CUSUM 变点检测与节奏偏移定位。"""
from __future__ import annotations

import warnings

import numpy as np
from scipy import stats

from .models import AnomalyInterval, NoteEvent, TempoCurve


def rolling_stationarity(
    curve: TempoCurve, window: int, pvalue: float
) -> list[AnomalyInterval]:
    """相邻滚动窗口均值 t 检验，拒绝平稳假设的网格点合并为区间。"""
    grid = curve.grid_beats
    tempos = curve.tempo_bpm
    flagged = np.zeros(len(grid), dtype=bool)
    for end in range(window * 2, len(grid) + 1):
        left = tempos[end - 2 * window : end - window]
        right = tempos[end - window : end]
        if np.std(left) + np.std(right) < 1e-9:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            _, p_value = stats.ttest_ind(left, right, equal_var=False)
        if np.isfinite(p_value) and p_value < pvalue:
            flagged[end - window : end] = True
    return _merge_flagged(
        grid, flagged, "robust_residual", "nonstationary", 0.0
    )


def cusum_changepoints(
    curve: TempoCurve,
    k: float = 0.5,
    h: float = 4.0,
    baseline_window: int = 8,
) -> list[dict]:
    """滚动基线均值漂移 CUSUM，返回变点位置与方向、幅度。

    每个点以其之前 baseline_window 个点估计局部均值与标准差，
    避免整曲均值归一掩盖真实漂移。
    """
    tempos = curve.tempo_bpm
    grid = curve.grid_beats
    positive = np.zeros_like(tempos)
    negative = np.zeros_like(tempos)
    changepoints: list[dict] = []
    run_start = 0
    for idx in range(1, len(tempos)):
        left = max(0, idx - baseline_window)
        baseline = tempos[left:idx]
        local_mean = float(np.mean(baseline))
        local_scale = float(np.std(baseline))
        if local_scale < 1e-9:
            local_scale = max(float(np.std(tempos)), 1e-6)
        standardized = (float(tempos[idx]) - local_mean) / local_scale
        positive[idx] = max(0.0, positive[idx - 1] + standardized - k)
        negative[idx] = max(0.0, negative[idx - 1] - standardized - k)
        if positive[idx] > h:
            changepoints.append(
                {
                    "beat": float(grid[idx]),
                    "direction": "accel",
                    "magnitude": float(positive[idx] * local_scale),
                    "start_idx": run_start,
                    "end_idx": idx,
                }
            )
            positive[idx] = 0.0
            run_start = idx
        elif negative[idx] > h:
            changepoints.append(
                {
                    "beat": float(grid[idx]),
                    "direction": "ritard",
                    "magnitude": float(negative[idx] * local_scale),
                    "start_idx": run_start,
                    "end_idx": idx,
                }
            )
            negative[idx] = 0.0
            run_start = idx
    return changepoints


def detect_tempo_jumps(
    curve: TempoCurve,
    jump_bpm: float,
    k: float,
    h: float,
    baseline_window: int = 8,
) -> list[AnomalyInterval]:
    """变点两侧平均速度差超过阈值时定位为速度突变区间。"""
    changepoints = cusum_changepoints(
        curve, k=k, h=h, baseline_window=baseline_window
    )
    grid = curve.grid_beats
    tempos = curve.tempo_bpm
    step = curve.grid_step_beats
    intervals: list[AnomalyInterval] = []
    for cp in changepoints:
        idx = int(np.searchsorted(grid, cp["beat"]))
        left_idx = max(0, idx - baseline_window)
        right_idx = min(len(grid), idx + 3)
        left_slice = tempos[left_idx : max(idx - 2, left_idx + 1)]
        right_slice = tempos[max(idx, idx + 0) : right_idx]
        if right_slice.size == 0:
            right_slice = tempos[idx : idx + 1]
        left_mean = float(np.mean(left_slice))
        right_mean = float(np.mean(right_slice))
        delta = right_mean - left_mean
        if abs(delta) < jump_bpm:
            continue
        direction = "accel" if delta > 0 else "ritard"
        intervals.append(
            AnomalyInterval(
                start_beat=max(0.0, cp["beat"] - step),
                end_beat=float(cp["beat"] + step),
                direction=direction,
                magnitude=float(abs(delta)),
                kind="tempo_jump",
                detail={"cusum_score": cp["magnitude"], "beat": cp["beat"]},
            )
        )
    return intervals


def detect_rhythm_offsets(
    events: list[NoteEvent],
    voice: str,
    reference_curve: dict[str, np.ndarray],
    offset_ratio: float,
) -> list[AnomalyInterval]:
    """相邻音间隔相对参考速度曲线期望间隔的偏差超过阈值时定位。"""
    ordered = sorted(
        (event for event in events if event.voice == voice),
        key=lambda event: event.onset_beat,
    )
    if len(ordered) < 2:
        return []
    ref_grid = reference_curve["grid_beats"]
    ref_tempo = reference_curve["tempo_bpm"]
    onsets = np.asarray([event.onset_beat for event in ordered])
    durations = np.asarray([event.duration_beats for event in ordered])
    ref_at_notes = np.interp(onsets, ref_grid, ref_tempo)
    expected_ioi = durations / (ref_at_notes / 60.0)
    actual_ioi = np.diff(onsets, prepend=onsets[0] - expected_ioi[0])
    with np.errstate(divide="ignore", invalid="ignore"):
        deviation = (actual_ioi - expected_ioi) / expected_ioi
    flagged = np.abs(deviation) > offset_ratio

    intervals: list[AnomalyInterval] = []
    start = None
    for idx, is_flagged in enumerate(flagged):
        if is_flagged and start is None:
            start = idx
        if start is not None and (not is_flagged or idx == len(flagged) - 1):
            end = idx if not is_flagged else idx + 1
            segment = deviation[start:end]
            mean_dev = float(np.mean(segment))
            direction = "drag" if mean_dev > 0 else "rush"
            intervals.append(
                AnomalyInterval(
                    start_beat=float(onsets[start]),
                    end_beat=float(
                        onsets[end - 1] + actual_ioi[min(end - 1, len(actual_ioi) - 1)]
                    ),
                    direction=direction,
                    magnitude=float(abs(mean_dev)),
                    kind="rhythm_offset",
                    detail={
                        "mean_deviation_ratio": mean_dev,
                        "n_notes": int(end - start),
                    },
                )
            )
            start = None
    return intervals


def _merge_flagged(
    grid: np.ndarray,
    flagged: np.ndarray,
    kind: str,
    direction: str,
    magnitude: float,
) -> list[AnomalyInterval]:
    intervals: list[AnomalyInterval] = []
    start = None
    for idx, is_flagged in enumerate(flagged):
        if is_flagged and start is None:
            start = idx
        if start is not None and (not is_flagged or idx == len(flagged) - 1):
            end = idx if not is_flagged else idx + 1
            intervals.append(
                AnomalyInterval(
                    start_beat=float(grid[start]),
                    end_beat=float(grid[min(end, len(grid) - 1)]),
                    direction=direction,
                    magnitude=magnitude,
                    kind=kind,
                )
            )
            start = None
    return intervals
