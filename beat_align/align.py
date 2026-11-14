"""把镜头起点对齐到最近鼓点（需求 4）。

偏移方向取值：

* ``earlier``（向前 / 负偏移）：对齐后起点早于原始起点；
* ``later``（向后 / 正偏移）：对齐后起点晚于原始起点；
* ``none``（零偏移）：原始起点恰好落在鼓点上。

等距时（两个鼓点与镜头起点毫秒差相等）确定性地选择更早的鼓点，
保证同一输入反复运行得到完全一致的结果。
"""

from __future__ import annotations

from dataclasses import dataclass

from .detect import Beat
from .shots import ShotEntry

DIRECTION_EARLIER = "earlier"
DIRECTION_LATER = "later"
DIRECTION_NONE = "none"

DIRECTION_LABELS = {
    DIRECTION_EARLIER: "向前(负偏移)",
    DIRECTION_LATER: "向后(正偏移)",
    DIRECTION_NONE: "无偏移",
}


@dataclass(frozen=True)
class AlignedShot:
    """一个镜头的对齐结果。"""

    entry: ShotEntry
    aligned_start_ms: int
    original_start_ms: int
    offset_ms: int
    """偏移量，恒为 ``|对齐后起点 - 原始起点|``（非负，需求 4）。"""
    direction: str
    beat_time_ms: int
    """实际使用的鼓点时间。"""

    @property
    def signed_offset_ms(self) -> int:
        """带符号偏移：负=向前，正=向后。"""
        if self.direction == DIRECTION_EARLIER:
            return -self.offset_ms
        if self.direction == DIRECTION_LATER:
            return self.offset_ms
        return 0


def nearest_beat(original_start_ms: int, beats: list[Beat]) -> tuple[int, int]:
    """返回 ``(鼓点时间毫秒, 偏移量毫秒)``，等距时取更早的鼓点。"""
    if not beats:
        raise ValueError("鼓点列表为空，无法执行对齐")

    best_beat = beats[0].time_ms
    best_gap = abs(beats[0].time_ms - original_start_ms)
    for beat in beats[1:]:
        gap = abs(beat.time_ms - original_start_ms)
        # 严格小于才替换：等距时保留先遍历到的、也就是更早的鼓点
        if gap < best_gap:
            best_gap = gap
            best_beat = beat.time_ms
    return best_beat, best_gap


def align_shots(entries: list[ShotEntry], beats: list[Beat]) -> list[AlignedShot]:
    """对所有未跳过镜头执行最近鼓点对齐，保持镜头清单顺序。"""
    results: list[AlignedShot] = []
    for entry in entries:
        if entry.skipped:
            continue
        beat_time, gap = nearest_beat(entry.original_start_ms, beats)
        if beat_time < entry.original_start_ms:
            direction = DIRECTION_EARLIER
        elif beat_time > entry.original_start_ms:
            direction = DIRECTION_LATER
        else:
            direction = DIRECTION_NONE
        results.append(
            AlignedShot(
                entry=entry,
                aligned_start_ms=beat_time,
                original_start_ms=entry.original_start_ms,
                offset_ms=gap,
                direction=direction,
                beat_time_ms=beat_time,
            )
        )
    return results
