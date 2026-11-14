"""帧区间冲突检测与重叠字幕的单行错位（泳道）排布。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameSpan:
    index: int
    start_frame: int
    end_frame: int


def find_inverted(spans: list[FrameSpan]) -> list[int]:
    """结束帧早于起始帧（倒置）的序号列表。"""
    return [s.index for s in spans if s.end_frame < s.start_frame]


def find_overlaps(spans: list[FrameSpan]) -> list[tuple[int, int]]:
    """检测帧区间相交的字幕对。

    帧区间按半开区间 [start, end) 处理（同帧首尾相接不算交叠），
    倒置区间不参与配对（倒置已单独报告）。返回的序号对按升序排列。
    """
    ordered = sorted(
        (s for s in spans if s.end_frame >= s.start_frame),
        key=lambda s: (s.start_frame, s.end_frame, s.index),
    )
    pairs: list[tuple[int, int]] = []
    for i, earlier in enumerate(ordered):
        for later in ordered[i + 1:]:
            if later.start_frame >= earlier.end_frame:
                break  # 已排序：再往后的起点只会更晚
            lo, hi = sorted((earlier.index, later.index))
            pairs.append((lo, hi))
    return pairs


def assign_lanes(spans: list[FrameSpan]) -> dict[int, int]:
    """区间图着色：同泳道内帧区间互不相交，泳道号小者位置靠下。"""
    ordered = sorted(
        (s for s in spans if s.end_frame >= s.start_frame),
        key=lambda s: (s.start_frame, s.index),
    )
    # lane_end[lane] = 该泳道当前最后一条字幕的结束帧
    lane_end: list[int] = []
    result: dict[int, int] = {}
    for span in ordered:
        placed = False
        for lane, end in enumerate(lane_end):
            if end <= span.start_frame:  # 半开区间：首尾相接可复用
                lane_end[lane] = span.end_frame
                result[span.index] = lane
                placed = True
                break
        if not placed:
            lane_end.append(span.end_frame)
            result[span.index] = len(lane_end) - 1
    return result
