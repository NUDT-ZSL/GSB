"""字幕校验与分配。

每条字幕独立判定，失败条目进入 unfinished 列表而不影响其他条目。
reason 取值固定（报告里原样输出，可机器比对）：
    reversed_range  起止倒序（end_ms <= start_ms）
    out_of_bounds   区间超出素材总长度，或起始为负
    empty_text      文本为空或只有空白字符
    unknown_shot    显式引用的镜头编号不存在
    outside_shot    区间没有被镜头完整覆盖（落在空档、跨越空档、或被覆盖镜头无帧）

「落在某镜头范围内」的判定：
    * 显式给了 shot_id —— 区间必须完整落在该镜头的有效区间内；
    * 未给 shot_id     —— 区间必须完整落在时间轴上某一段连续有画面的区间内。
      连续有画面可以是多个相邻镜头拼起来的，因此跨镜头（跨切点）的字幕是合法的，
      这正是需求 3 要求的：切点前后显示同一文本。
空档把连续区间打断，所以任何碰触空档的字幕都会被拒绝，保证空档帧绝不会出现字幕。
"""

from __future__ import annotations

from bisect import bisect_right

from .models import ComposedSubtitle, Gap, PlacedShot, Subtitle, UnfinishedItem
from .timeline import total_duration_ms

REASON_REVERSED = "reversed_range"
REASON_OUT_OF_BOUNDS = "out_of_bounds"
REASON_EMPTY_TEXT = "empty_text"
REASON_UNKNOWN_SHOT = "unknown_shot"
REASON_OUTSIDE_SHOT = "outside_shot"

# 报告里的分类顺序固定为这个元组，不受输入顺序影响
REASON_ORDER = (
    REASON_REVERSED,
    REASON_OUT_OF_BOUNDS,
    REASON_EMPTY_TEXT,
    REASON_UNKNOWN_SHOT,
    REASON_OUTSIDE_SHOT,
)

REASON_LABELS = {
    REASON_REVERSED: "起止倒序",
    REASON_OUT_OF_BOUNDS: "区间越界",
    REASON_EMPTY_TEXT: "文本为空",
    REASON_UNKNOWN_SHOT: "镜头编号不存在",
    REASON_OUTSIDE_SHOT: "未被镜头覆盖",
}


def coverage_intervals(placed: list[PlacedShot]) -> list[tuple[int, int]]:
    """把参与渲染的镜头区间合并成「连续有画面」的区间列表。"""
    segments = sorted(
        (p.effective_start_ms, p.effective_end_ms)
        for p in placed
        if p.effective_end_ms > p.effective_start_ms
    )
    merged: list[tuple[int, int]] = []
    for start, end in segments:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _gap_touching(subtitle: Subtitle, gaps: list[Gap]) -> Gap | None:
    for gap in gaps:
        if gap.start_ms < subtitle.end_ms and subtitle.start_ms < gap.end_ms:
            return gap
    return None


def _describe_outside(subtitle: Subtitle, placed: list[PlacedShot], gaps: list[Gap]) -> str:
    gap = _gap_touching(subtitle, gaps)
    if gap is not None:
        return (
            f"区间 {subtitle.start_ms}–{subtitle.end_ms}ms 与空档 "
            f"{gap.start_ms}–{gap.end_ms}ms 相交，空档不承载字幕"
        )
    return f"区间 {subtitle.start_ms}–{subtitle.end_ms}ms 未被任何连续镜头区间完整覆盖"


def classify(
    subtitle: Subtitle,
    placed: list[PlacedShot],
    gaps: list[Gap],
    coverage: list[tuple[int, int]],
) -> tuple[ComposedSubtitle | None, UnfinishedItem | None]:
    total_ms = total_duration_ms(placed)

    if subtitle.end_ms <= subtitle.start_ms:
        return None, UnfinishedItem(
            REASON_REVERSED,
            subtitle,
            f"end_ms({subtitle.end_ms}) 不大于 start_ms({subtitle.start_ms})",
        )
    if subtitle.start_ms < 0 or subtitle.end_ms > total_ms:
        return None, UnfinishedItem(
            REASON_OUT_OF_BOUNDS,
            subtitle,
            f"区间 {subtitle.start_ms}–{subtitle.end_ms}ms 超出素材总长度 0–{total_ms}ms",
        )
    if not subtitle.text.strip():
        return None, UnfinishedItem(REASON_EMPTY_TEXT, subtitle, "文本为空或仅含空白字符")

    by_id = {p.shot.shot_id: p for p in placed}
    if subtitle.shot_id is not None:
        target = by_id.get(subtitle.shot_id)
        if target is None:
            known = "、".join(sorted(by_id))
            return None, UnfinishedItem(
                REASON_UNKNOWN_SHOT,
                subtitle,
                f"引用镜头 {subtitle.shot_id!r}，镜头清单中只有：{known}",
            )
        if target.effective_end_ms <= target.effective_start_ms:
            return None, UnfinishedItem(
                REASON_OUTSIDE_SHOT,
                subtitle,
                f"引用镜头 {subtitle.shot_id!r} 与更早镜头完全重叠，没有任何参与渲染的帧",
            )
        if (
            subtitle.start_ms < target.effective_start_ms
            or subtitle.end_ms > target.effective_end_ms
        ):
            note = ""
            if target.effective_start_ms != target.shot.start_ms:
                note = (
                    f"；该镜头头部与更早镜头重叠，实际有效区间为 "
                    f"{target.effective_start_ms}–{target.effective_end_ms}ms"
                )
            return None, UnfinishedItem(
                REASON_OUTSIDE_SHOT,
                subtitle,
                f"区间 {subtitle.start_ms}–{subtitle.end_ms}ms 未完整落在镜头 "
                f"{subtitle.shot_id!r} 的有效区间 "
                f"{target.effective_start_ms}–{target.effective_end_ms}ms 内{note}",
            )
    else:
        if not any(lo <= subtitle.start_ms and subtitle.end_ms <= hi for lo, hi in coverage):
            return None, UnfinishedItem(
                REASON_OUTSIDE_SHOT,
                subtitle,
                _describe_outside(subtitle, placed, gaps),
            )

    covered = tuple(
        p.shot.shot_id
        for p in placed
        if p.effective_end_ms > p.effective_start_ms
        and p.effective_start_ms < subtitle.end_ms
        and subtitle.start_ms < p.effective_end_ms
    )
    return ComposedSubtitle(subtitle=subtitle, shot_ids=covered), None


def classify_all(
    subtitles: list[Subtitle], placed: list[PlacedShot], gaps: list[Gap]
) -> tuple[list[ComposedSubtitle], list[UnfinishedItem]]:
    coverage = coverage_intervals(placed)
    composed: list[ComposedSubtitle] = []
    unfinished: list[UnfinishedItem] = []
    for subtitle in subtitles:
        ok, bad = classify(subtitle, placed, gaps, coverage)
        if ok is not None:
            composed.append(ok)
        else:
            assert bad is not None
            unfinished.append(bad)
    # 输出顺序固定：先按原因枚举顺序，再按字幕原始下标
    reason_rank = {r: i for i, r in enumerate(REASON_ORDER)}
    unfinished.sort(key=lambda u: (reason_rank[u.reason], u.subtitle.index))
    return composed, unfinished


class SubtitleIndex:
    """按时间查询当前应显示的字幕文本。渲染与报告共用同一个索引实例。"""

    def __init__(self, composed: list[ComposedSubtitle]) -> None:
        self._items = sorted(
            composed, key=lambda c: (c.subtitle.start_ms, c.subtitle.end_ms, c.subtitle.index)
        )
        self._starts = [c.subtitle.start_ms for c in self._items]

    def texts_at(self, time_ms: int) -> tuple[str, ...]:
        # 半开区间 [start, end)：切点落在 end 时该条已结束，落在 start 时刚开始，
        # 因此相邻两条不会在同一帧重复叠加。
        limit = bisect_right(self._starts, time_ms)
        return tuple(c.subtitle.text for c in self._items[:limit] if time_ms < c.subtitle.end_ms)
