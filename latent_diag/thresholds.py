"""边界判定与 top-k 候选的数值稳定性保护。

提供两种比较策略：
  - strict         : 裸比较 value > threshold
  - tolerance_band : 容差带比较。门限附近 ±band 内的值先吸附到门限
                     再比较，并用 math.nextafter 扩展带边界，
                     保证恰好落在门限附近的元素在重复运行与多精度档下
                     判定不被舍入翻转。
次正规数保护：|v| 小于 float64 最小正规数时按 0 处理，避免次正规
算术在不同平台/精度档下的非确定行为。
"""

from __future__ import annotations

import math

import numpy as np

from .config import STRATEGY_STRICT, STRATEGY_TOLERANCE_BAND

_MIN_NORMAL = np.finfo(np.float64).tiny


def subnormal_guard(value: float) -> float:
    """次正规数保护：次正规量级的值按 0 处理。"""
    if -_MIN_NORMAL < value < _MIN_NORMAL:
        return 0.0
    return value


def _band_edges(threshold: float, band: float):
    half = max(band, band * abs(threshold))
    lo = math.nextafter(threshold - half, -math.inf)
    hi = math.nextafter(threshold + half, math.inf)
    return lo, hi


def exceeds_threshold(value: float, threshold: float, policy) -> bool:
    """按策略判定 value 是否越过门限。确定性、抗舍入翻转。"""
    value = subnormal_guard(float(value))
    threshold = float(threshold)
    if policy.compare_strategy == STRATEGY_STRICT:
        return value > threshold
    if policy.compare_strategy == STRATEGY_TOLERANCE_BAND:
        lo, hi = _band_edges(threshold, policy.tolerance_band)
        if lo <= value <= hi:
            # 容差带内：吸附到门限后判定，结果确定且不越界
            return False
        return value > hi
    raise ValueError(f"unknown compare_strategy: {policy.compare_strategy}")


def classify_residuals(residuals, threshold: float, policy) -> dict:
    """对一组边界残差做门限分类，返回标记与翻转风险统计。"""
    vals = np.asarray(residuals, dtype=np.float64).ravel()
    flags = np.array([exceeds_threshold(v, threshold, policy) for v in vals],
                     dtype=bool)
    lo, hi = _band_edges(float(threshold), policy.tolerance_band)
    guarded = np.array([subnormal_guard(v) for v in vals])
    flip_risk = int(np.count_nonzero((guarded >= lo) & (guarded <= hi)))
    return {
        "count": int(vals.size),
        "exceed_count": int(flags.sum()),
        "flip_risk_count": flip_risk,
        "max_residual": float(np.abs(vals).max()) if vals.size else 0.0,
        "strategy": policy.compare_strategy,
    }


def top_k_candidates(values, k: int, policy) -> list:
    """稳定 top-k：按容差带吸附后的键值降序、索引升序决胜。

    返回候选列表 [{index, value, key, above_threshold}]，
    排序确定性不依赖数组布局或平台舍入细节。
    """
    vals = np.asarray(values, dtype=np.float64).ravel()
    band = policy.tolerance_band
    keys = []
    for idx, v in enumerate(vals):
        v = subnormal_guard(float(v))
        if policy.compare_strategy == STRATEGY_TOLERANCE_BAND and band > 0:
            key = round(v / band) * band  # 吸附到容差带网格
        else:
            key = v
        keys.append((idx, v, key))
    keys.sort(key=lambda t: (-t[2], t[0]))
    top = keys[:max(0, int(k))]
    return [
        {
            "index": idx,
            "value": v,
            "key": key,
            "above_threshold": exceeds_threshold(
                abs(v), policy.decode_candidate_threshold, policy),
        }
        for idx, v, key in top
    ]
