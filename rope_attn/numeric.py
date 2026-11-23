"""数值稳定性工具：舍入模式、次正规数保护与统计量化。

分块最大值与归一化分母始终以 float64 累加；当 ``stat_dtype=float32``
时，每个分块统计量落盘前按配置的舍入模式量化，用于可配置口径验证。
float64 路径下舍入模式为恒等（默认 round-to-nearest-even 即原生语义）。
"""

from __future__ import annotations

from typing import Iterable

import numpy as np


def is_subnormal(value: np.ndarray) -> np.ndarray:
    info = np.finfo(value.dtype)
    return np.isfinite(value) & (np.abs(value) < info.tiny)


def flush_subnormals(value: np.ndarray) -> np.ndarray:
    """把次正规数（含有符号零附近的极小值）冲零。"""
    result = np.array(value, copy=True)
    result[is_subnormal(result)] = 0.0
    return result


def quantize(
    value: np.ndarray | float,
    dtype: str,
    rounding: str = "half_to_even",
    subnormal_protection: bool = True,
) -> np.ndarray | float:
    """按声明的舍入模式把数值量化到目标 dtype。

    模式：
        half_to_even : 原生银行家舍入（默认）；
        truncate     : 向零截断；
        down         : 向负无穷；
        half_up      : 四舍五入（半值向远离零）。
    """
    target = np.dtype(dtype)
    scalar = np.isscalar(value)
    array = np.asarray(value, dtype=np.float64)
    if target == np.float64:
        result = array
    else:
        result = _round_to_float32(array, rounding)

    if subnormal_protection:
        result = flush_subnormals(result)
    return float(result) if scalar else np.ascontiguousarray(result)


def _round_to_float32(array: np.ndarray, rounding: str) -> np.ndarray:
    """以 float32 相邻可表示数为网格做显式舍入。

    先确定每个值的 floor/ceil 两个相邻 float32 网格点，再按声明模式：
        truncate 向零；down 向负无穷；half_up 半值向远离零。
    """
    inf32 = np.asarray(np.inf, dtype=np.float32)
    nearest = array.astype(np.float32)
    if rounding == "half_to_even":
        return nearest

    nearest64 = nearest.astype(np.float64)
    neighbor_down = np.nextafter(nearest, -inf32).astype(np.float64)
    neighbor_up = np.nextafter(nearest, inf32).astype(np.float64)
    floor_grid = np.where(nearest64 <= array, nearest64, neighbor_down)
    ceil_grid = np.where(nearest64 >= array, nearest64, neighbor_up)
    midpoint = (floor_grid + ceil_grid) * 0.5

    if rounding == "truncate":
        chosen = np.where(array >= 0.0, floor_grid, ceil_grid)
    elif rounding == "down":
        chosen = floor_grid
    elif rounding == "half_up":
        # 半值向远离零：正数 >= 中点取 ceil，负数 <= 中点取 floor。
        chosen = np.where(
            array >= 0.0,
            np.where(array >= midpoint, ceil_grid, floor_grid),
            np.where(array <= midpoint, floor_grid, ceil_grid),
        )
    else:
        raise ValueError(f"未知舍入模式 {rounding!r}")
    return chosen.astype(np.float32)


def stable_sum(values: Iterable[float]) -> float:
    """顺序确定的 float64 求和（Kahan 补偿），保证逐字节可复现。"""
    total = 0.0
    compensation = 0.0
    for value in values:
        value = float(value) - compensation
        new_total = total + value
        compensation = (new_total - total) - value
        total = new_total
    return total
