"""审计流水线：帧换算、冲突检测、换行排版、边距计算、错位排布汇总为记录。"""
from __future__ import annotations

from dataclasses import dataclass

from .arrangement import FrameSpan, assign_lanes, find_inverted, find_overlaps
from .config import CanvasConfig
from .layout import TextLayout, wrap_text
from .parser import ParseError, ParsedCue
from .timing import frame_number


@dataclass
class CueRecord:
    index: int
    time_text: str
    text: str
    start_ms: int
    end_ms: int
    start_frame: int
    end_frame: int
    inverted: bool
    line_count: int
    lines: list[dict]
    max_width_px: int
    block_height_px: int
    exceeds_safe_width: bool
    lane: int | None
    rect: dict            # x0/y0/x1/y1，代表帧中的实际像素位置
    margins: dict         # left/right/top/bottom 四向边距
    outside_safe: bool
    overflow_directions: list[str]
    conflict_flags: list[str]
    overlap_partners: list[int]


def _build_record(cue: ParsedCue, cfg: CanvasConfig, layout: TextLayout,
                  lane: int | None, slot_bottom: int, slot_height: int,
                  inverted: bool, partners: list[int]) -> CueRecord:
    block_w = layout.max_width_px
    x0 = cfg.safe_x + (cfg.safe_w - block_w) // 2
    x1 = x0 + block_w
    # 块在自身泳道槽位内垂直居中，保证多句同屏时纵向严格分离
    y1 = slot_bottom - max(0, (slot_height - layout.block_height_px) // 2)
    y0 = y1 - layout.block_height_px

    margins = {
        "left": x0,
        "right": cfg.width - x1,
        "top": y0,
        "bottom": cfg.height - y1,
    }
    directions: list[str] = []
    if x0 < cfg.safe_x:
        directions.append("左")
    if x1 > cfg.width - cfg.safe_x:
        directions.append("右")
    if y0 < cfg.safe_y:
        directions.append("上")
    if y1 > cfg.height - cfg.safe_y:
        directions.append("下")

    flags: list[str] = []
    if inverted:
        flags.append("倒置")
    if partners:
        flags.append("帧区间交叠")
    if directions:
        flags.append("越出安全区")

    return CueRecord(
        index=cue.index,
        time_text=cue.time_text,
        text=cue.text,
        start_ms=cue.start_ms,
        end_ms=cue.end_ms,
        start_frame=frame_number(cue.start_ms, cfg.fps),
        end_frame=frame_number(cue.end_ms, cfg.fps),
        inverted=inverted,
        line_count=layout.line_count,
        lines=[{"text": ln.text, "width_px": ln.width_px} for ln in layout.lines],
        max_width_px=layout.max_width_px,
        block_height_px=layout.block_height_px,
        exceeds_safe_width=layout.exceeds_safe_width,
        lane=lane,
        rect={"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        margins=margins,
        outside_safe=bool(directions),
        overflow_directions=directions,
        conflict_flags=flags,
        overlap_partners=partners,
    )


def run_audit(cues: list[ParsedCue], parse_errors: list[ParseError],
              cfg: CanvasConfig, font) -> dict:
    # 1) 帧换算与倒置检测
    spans = [
        FrameSpan(c.index, frame_number(c.start_ms, cfg.fps),
                  frame_number(c.end_ms, cfg.fps))
        for c in cues
    ]
    inverted_set = set(find_inverted(spans))
    overlap_pairs = find_overlaps(spans)
    lanes = assign_lanes(spans)

    # 每条字幕的交叠对手
    partners: dict[int, list[int]] = {}
    for a, b in overlap_pairs:
        partners.setdefault(a, []).append(b)
        partners.setdefault(b, []).append(a)

    # 2) 换行排版
    layouts: dict[int, TextLayout] = {
        cue.index: wrap_text(cue.text, font, cfg.safe_w, cfg.line_height)
        for cue in cues
    }

    # 3) 泳道纵向槽位：每个泳道的槽高取该道内最大字幕块高度，
    #    自安全区底边向上堆叠；道间留一个行高的间隙，包围框/标签不压相邻字幕
    lane_gap = cfg.line_height
    lane_heights: dict[int, int] = {}
    for cue in cues:
        if cue.index in inverted_set:
            continue
        lane = lanes[cue.index]
        lane_heights[lane] = max(
            lane_heights.get(lane, 0), layouts[cue.index].block_height_px
        )
    slot_bottoms: dict[int, int] = {}
    cursor = cfg.height - cfg.safe_y
    for lane in sorted(lane_heights):
        slot_bottoms[lane] = cursor
        # 槽高即该道最大块高；额外行高间隙只加在道与道之间，首道仍贴安全区底边
        cursor -= lane_heights[lane] + (lane_gap if lane > min(lane_heights) else 0)

    # 4) 生成逐条记录
    records: list[CueRecord] = []
    for cue in cues:
        if cue.index in inverted_set:
            lane, bottom, slot_h = None, cfg.height - cfg.safe_y, layouts[cue.index].block_height_px
        else:
            lane = lanes[cue.index]
            bottom = slot_bottoms[lane]
            slot_h = lane_heights[lane]
        records.append(
            _build_record(
                cue, cfg, layouts[cue.index], lane, bottom, slot_h,
                inverted=cue.index in inverted_set,
                partners=sorted(partners.get(cue.index, [])),
            )
        )

    # 5) 统计：异常 = 坏块 ∪ 倒置 ∪ 越界 ∪ 涉交叠 的并集
    anomalous_indexes = set(inverted_set)
    for a, b in overlap_pairs:
        anomalous_indexes.update((a, b))
    for rec in records:
        if rec.outside_safe:
            anomalous_indexes.add(rec.index)
    anomalous_count = len(anomalous_indexes) + len(parse_errors)

    return {
        "records": records,
        "parse_errors": parse_errors,
        "overlap_pairs": overlap_pairs,
        "inverted_indexes": sorted(inverted_set),
        "anomalous_count": anomalous_count,
        "total_blocks": len(cues) + len(parse_errors),
    }
