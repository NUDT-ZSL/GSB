"""镜头关键帧插值（需求 2、4）。

- 相邻关键帧之间：缩放与中心点按时间线性变化；
- 首帧之前 / 末帧之后：保持最近帧的值（钳制）。
"""
from __future__ import annotations

from .model import FIT_CLOSE, FIT_CLOSE_MAX_MS, FIT_OFF, FIT_TIGHT, FIT_TIGHT_MAX_MS, Keyframe


def camera_at(keyframes: list[Keyframe], t: float) -> tuple[float, float, float]:
    """返回时刻 t 的 (zoom, cx, cy)。keyframes 已按时刻升序且非空。"""
    first = keyframes[0]
    last = keyframes[-1]
    if t <= first.time_ms:
        return first.zoom, first.cx, first.cy
    if t >= last.time_ms:
        return last.zoom, last.cx, last.cy

    for i in range(len(keyframes) - 1):
        a = keyframes[i]
        b = keyframes[i + 1]
        if a.time_ms <= t <= b.time_ms:
            if b.time_ms == a.time_ms:
                ratio = 0.0
            else:
                ratio = (t - a.time_ms) / (b.time_ms - a.time_ms)
            return (
                a.zoom + (b.zoom - a.zoom) * ratio,
                a.cx + (b.cx - a.cx) * ratio,
                a.cy + (b.cy - a.cy) * ratio,
            )
    # 理论不可达：钳制保护
    return last.zoom, last.cx, last.cy


def nearest_keyframe_gap(
    keyframes: list[Keyframe], t: float
) -> tuple[float, float]:
    """返回 (与最近关键帧的毫秒间隔, 该关键帧时刻)。"""
    nearest = min(keyframes, key=lambda kf: abs(kf.time_ms - t))
    return abs(t - nearest.time_ms), nearest.time_ms


def fit_level(gap_ms: float) -> str:
    if gap_ms <= FIT_TIGHT_MAX_MS:
        return FIT_TIGHT
    if gap_ms <= FIT_CLOSE_MAX_MS:
        return FIT_CLOSE
    return FIT_OFF
