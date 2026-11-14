"""核心数据结构与常量。"""
from __future__ import annotations

from dataclasses import dataclass

# 贴合度阈值（毫秒）：音效时刻与最近关键帧的间隔
FIT_TIGHT_MAX_MS = 50.0
FIT_CLOSE_MAX_MS = 150.0

FIT_TIGHT = "tight"   # 间隔 <= 50ms
FIT_CLOSE = "close"   # 50ms < 间隔 <= 150ms
FIT_OFF = "off"       # 间隔 > 150ms

FIT_LABELS = {
    FIT_TIGHT: "精准",
    FIT_CLOSE: "贴近",
    FIT_OFF: "偏离",
}


def is_number(value: object) -> bool:
    """JSON 意义上的数字（bool 在 Python 是 int 子类，需排除）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Keyframe:
    """镜头关键帧：时刻（毫秒）、缩放、中心点 (x, y)。"""

    time_ms: float
    zoom: float
    cx: float
    cy: float


@dataclass(frozen=True)
class Sound:
    """音效落点。越界音效除 t/label 外均为空。"""

    t: float
    label: str
    out_of_bounds: bool = False
    zoom: float | None = None
    cx: float | None = None
    cy: float | None = None
    gap_ms: float | None = None
    nearest_kf_ms: float | None = None
    fit: str | None = None
