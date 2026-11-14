"""规范化镜头时间轴。

规则（见 README「时间轴规则」）：
1. 镜头按 (起始毫秒, 结束毫秒, 输入顺序) 升序排序，得到规范序列。
2. 任一镜头若与更早镜头重叠，其参与渲染的区间从「更早镜头的结束时刻」开始；
   完全被更早镜头覆盖的镜头不产生任何帧，状态记为 fully_overlapped。
3. 相邻参与渲染的区间之间若有距离，这段是空档（gap），渲染为空画面占位。
4. 素材总长度 = 最后一个参与渲染区间的结束时刻；起始时刻之前的空白也算空档。
"""

from __future__ import annotations

from bisect import bisect_right

from .models import Conflict, Gap, PlacedShot, Shot

STATUS_OK = "ok"
STATUS_HEAD_CLAMPED = "head_clamped"
STATUS_FULLY_OVERLAPPED = "fully_overlapped"


def canonical_order(shots: list[Shot]) -> list[Shot]:
    return sorted(shots, key=lambda s: (s.start_ms, s.end_ms, s.input_index))


def build_timeline(shots: list[Shot]) -> tuple[list[PlacedShot], list[Gap], list[Conflict]]:
    ordered = canonical_order(shots)
    placed: list[PlacedShot] = []
    cursor = 0
    for shot in ordered:
        effective_start = max(shot.start_ms, cursor)
        if effective_start >= shot.end_ms:
            placed.append(PlacedShot(shot, shot.start_ms, shot.start_ms, STATUS_FULLY_OVERLAPPED))
            continue
        status = STATUS_OK if effective_start == shot.start_ms else STATUS_HEAD_CLAMPED
        placed.append(PlacedShot(shot, effective_start, shot.end_ms, status))
        cursor = shot.end_ms

    # 冲突对：按规范顺序收集所有原始区间重叠的镜头对，earlier 为起始更早者。
    conflicts: list[Conflict] = []
    for i, earlier in enumerate(ordered):
        for later in ordered[i + 1 :]:
            if later.start_ms >= earlier.end_ms:
                continue
            conflicts.append(
                Conflict(
                    earlier=earlier,
                    later=later,
                    overlap_start_ms=later.start_ms,
                    overlap_end_ms=min(earlier.end_ms, later.end_ms),
                )
            )

    # 空档：参与渲染的区间之间有距离才是空档；完全重叠的镜头不产生区间。
    segments = [p for p in placed if p.effective_end_ms > p.effective_start_ms]
    gaps: list[Gap] = []
    if segments:
        cursor = 0
        for seg in segments:
            if seg.effective_start_ms > cursor:
                gaps.append(Gap(cursor, seg.effective_start_ms))
            cursor = seg.effective_end_ms
    return placed, gaps, conflicts


def total_duration_ms(placed: list[PlacedShot]) -> int:
    ends = [p.effective_end_ms for p in placed if p.effective_end_ms > p.effective_start_ms]
    return max(ends) if ends else 0


def segment_at(placed: list[PlacedShot], gaps: list[Gap], time_ms: int):
    """返回 time_ms 落在的片段：PlacedShot 或 Gap。"""
    return SegmentMap(placed, gaps).at(time_ms)


class SegmentMap:
    """按起始时刻二分的片段查找表，渲染逐帧查询用。

    空档与镜头区间互不重叠，合并排序后直接二分即可。
    """

    def __init__(self, placed: list[PlacedShot], gaps: list[Gap]) -> None:
        entries: list[tuple[int, int, object]] = [(g.start_ms, g.end_ms, g) for g in gaps]
        entries.extend(
            (p.effective_start_ms, p.effective_end_ms, p)
            for p in placed
            if p.effective_end_ms > p.effective_start_ms
        )
        entries.sort(key=lambda item: item[0])
        self._starts = [item[0] for item in entries]
        self._entries = entries

    def at(self, time_ms: int):
        index = bisect_right(self._starts, time_ms) - 1
        if index < 0:
            return None
        start, end, payload = self._entries[index]
        if start <= time_ms < end:
            return payload
        return None


def shot_index(placed: list[PlacedShot]) -> dict[str, PlacedShot]:
    return {p.shot.shot_id: p for p in placed}
