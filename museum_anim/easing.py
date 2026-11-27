"""缓动曲线与 Catmull-Rom 样条插值（统一、确定性的实现）。"""

import numpy as np

from .enums import Easing


def eased_progress(progress: float, easing: Easing) -> float:
    """把归一化时间 progress∈[0,1] 映射为缓动进度值。"""
    progress = float(np.clip(progress, 0.0, 1.0))
    if easing == Easing.LINEAR:
        return progress
    if easing == Easing.EASE_IN_OUT:
        # smoothstep：端点零速度
        return progress * progress * (3.0 - 2.0 * progress)
    if easing == Easing.SINE:
        # 正弦缓动：沿 sin 曲线从 0 到 1
        return np.sin(progress * np.pi / 2.0)
    raise ValueError(f"未知缓动类型: {easing}")


def catmull_rom_sample(points: np.ndarray, u: float) -> np.ndarray:
    """在等距控制点序列上按参数 u∈[0,1] 做均匀 Catmull-Rom 插值。

    端点使用钳制（clamp）方式构造虚拟控制点，保证曲线穿过首尾点。
    """
    points = np.asarray(points, dtype=np.float64)
    count = len(points)
    if count == 1:
        return points[0].copy()
    position = np.clip(u, 0.0, 1.0) * (count - 1)
    i = int(np.floor(position))
    if i >= count - 1:
        return points[-1].copy()
    local_t = position - i
    p0 = points[max(i - 1, 0)]
    p1 = points[i]
    p2 = points[min(i + 1, count - 1)]
    p3 = points[min(i + 2, count - 1)]
    t2 = local_t * local_t
    t3 = t2 * local_t
    return 0.5 * (
        2.0 * p1
        + (-p0 + p2) * local_t
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
    )


def resample_uniform(points: np.ndarray, sample_count: int) -> np.ndarray:
    """把控制点序列重采样为 sample_count 个等参数位置的点。"""
    if sample_count <= 1:
        return np.asarray(points[:1], dtype=np.float64)
    us = np.linspace(0.0, 1.0, sample_count)
    return np.vstack([catmull_rom_sample(points, u) for u in us])


def arc_length_reparametrize(points: np.ndarray, sample_count: int) -> np.ndarray:
    """按累积弧长重参数化后重采样，得到近似等弧长（匀速）的点序列。

    用于平滑修正：不改变首末位置与总时长，仅调整局部时间分布。
    """
    points = np.asarray(points, dtype=np.float64)
    if len(points) <= 2 or sample_count <= 1:
        return resample_uniform(points, sample_count)
    dense = resample_uniform(points, max(sample_count * 8, 64))
    seg_len = np.linalg.norm(np.diff(dense, axis=0), axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(seg_len)])
    total = cumulative[-1]
    if total <= 1e-12:
        return np.repeat(points[:1], sample_count, axis=0)
    target = np.linspace(0.0, total, sample_count)
    dense_index = np.searchsorted(cumulative, target, side="right") - 1
    dense_index = np.clip(dense_index, 0, len(dense) - 2)
    starts = cumulative[dense_index]
    denom = seg_len[dense_index]
    denom[denom <= 1e-12] = 1.0
    frac = (target - starts) / denom
    return dense[dense_index] + frac[:, None] * (dense[dense_index + 1] - dense[dense_index])
