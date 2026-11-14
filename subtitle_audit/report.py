"""报告输出：明细 CSV（需求 6）与对照 PNG（需求 5）。"""

from __future__ import annotations

import csv
from pathlib import Path

from .model import (
    INVALID_STATUS,
    OUT_OF_BOUNDS,
    OVERLAP,
    VALID,
    AuditRow,
    Shot,
    frame_to_ms,
    natural_key,
)
from .pngcanvas import GdiCanvas

# 需求 6：列顺序固定
CSV_HEADER = ["字幕ID", "文本", "起始帧", "结束帧", "起始毫秒", "结束毫秒",
              "归属镜头", "状态", "冲突描述"]

# 配色取自 dataviz 参考状态色板；红/绿在色觉障碍下依赖文字标签与斜纹第二编码，
# CSV 表视图作为兜底（见 README 设计说明）。
SURFACE = (252, 252, 251)
INK = (11, 11, 11)
INK_MUTED = (82, 81, 78)
GREEN = (12, 163, 12)          # 有效 good #0ca30c
RED = (208, 59, 59)            # 越界 critical #d03b3b
ORANGE = (236, 131, 90)        # 重叠 serious #ec835a
GRAY = (160, 158, 150)         # 无效
SHOT_FILL = (42, 120, 214)     # 镜头轴蓝 categorical slot1 #2a78d6
SKIP_FILL = (200, 199, 190)
GRID = (225, 224, 217)
BORDER = (195, 194, 183)


# --------------------------------------------------------------------------- #
# CSV（需求 6、7）
# --------------------------------------------------------------------------- #
def write_csv(rows: list[AuditRow], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\r\n")
        writer.writerow(CSV_HEADER)
        for row in rows:
            sub = row.subtitle
            if not row.valid_input:
                # 需求 7：状态为空，冲突描述提示“起止帧无效”
                writer.writerow([
                    sub.sid, sub.text, sub.start_frame, sub.end_frame,
                    "", "", "", INVALID_STATUS, row.invalid_reason,
                ])
                continue
            start_ms = frame_to_ms(sub.start_frame, row.fps) if row.fps is not None else ""
            end_ms = frame_to_ms(sub.end_frame, row.fps) if row.fps is not None else ""
            owner = row.owner.shot_id if row.owner is not None else ""
            writer.writerow([
                sub.sid, sub.text, sub.start_frame, sub.end_frame,
                start_ms, end_ms, owner, row.status, row.conflict,
            ])


# --------------------------------------------------------------------------- #
# PNG 对照图（需求 5）
# --------------------------------------------------------------------------- #
def render_png(rows: list[AuditRow], shots: list[Shot], path: str | Path) -> None:
    all_shots = sorted(shots, key=lambda s: (s.start_frame, natural_key(s.shot_id)))
    active = [s for s in all_shots if not s.skipped]
    timeline_lo = min(s.start_frame for s in all_shots)
    timeline_hi = max(s.end_frame for s in all_shots)
    # 字幕可能越界到镜头轴之外，绘图范围要容纳全部字幕
    for row in rows:
        timeline_lo = min(timeline_lo, row.subtitle.start_frame)
        timeline_hi = max(timeline_hi, row.subtitle.end_frame)
    span = max(1, timeline_hi - timeline_lo)

    margin_left, margin_right, margin_top = 170, 40, 20
    label_w = 150
    row_h, bar_h, shot_h = 28, 16, 26
    title_h, legend_h, axis_h, section_gap = 34, 30, 24, 18

    plot_w = min(1400, max(560, span * 2))
    width = margin_left + plot_w + margin_right
    height = (
        margin_top + title_h + legend_h
        + section_gap + shot_h + 10
        + axis_h
        + max(1, len(rows)) * row_h + 64
    )

    def x_of(frame: int) -> int:
        return margin_left + int(round((frame - timeline_lo) / span * plot_w))

    with GdiCanvas(width, height, SURFACE) as cv:
        y = margin_top
        # ---- 标题 ----
        cv.set_font(18, bold=True)
        cv.draw_text(margin_left, y, "对白镜头字幕对照图（帧区间半开 [start, end)）", INK)
        y += title_h

        # ---- 图例（颜色 + 文字双编码，不依赖颜色单独表意）----
        cv.set_font(14)
        legend = [
            (GREEN, "有效"),
            (RED, "越界"),
            (ORANGE, "重叠（斜纹）"),
            (GRAY, "无效输入"),
            (SHOT_FILL, "镜头时间轴"),
        ]
        lx = margin_left
        ly = y + 4
        for color, name in legend:
            cv.fill_rect(lx, ly, 18, 14, color)
            cv.stroke_rect(lx, ly, 18, 14, BORDER, 1)
            tw, _ = cv.text_size(name)
            cv.draw_text(lx + 24, ly, name, INK_MUTED)
            lx += 24 + tw + 26
        y += legend_h + section_gap

        # ---- 镜头横条 ----
        cv.set_font(14, bold=True)
        cv.draw_text(16, y + 4, "镜头时间轴", INK)
        cv.set_font(13)
        for shot in all_shots:
            x0, x1 = x_of(shot.start_frame), x_of(shot.end_frame)
            fill = SKIP_FILL if shot.skipped else SHOT_FILL
            cv.fill_rect(x0, y, max(2, x1 - x0), shot_h, fill)
            cv.stroke_rect(x0, y, max(2, x1 - x0), shot_h, BORDER, 1)
            label = f"{shot.shot_id} [{shot.start_frame},{shot.end_frame})"
            if shot.skipped:
                label += " 重叠剔除"
            cv.draw_text(x0 + 4, y + 5, label, INK if not shot.skipped else INK_MUTED)
        y += shot_h + 10

        # ---- 帧刻度 ----
        cv.set_font(12)
        for tick in range(6):
            frame = timeline_lo + round(span * tick / 5)
            tx = x_of(frame)
            cv.draw_line(tx, y, tx, y + axis_h - 8, BORDER, 1)
            label = str(frame)
            tw, _ = cv.text_size(label)
            cv.draw_text(tx - tw // 2, y + axis_h - 6, label, INK_MUTED)
        y += axis_h

        # ---- 镜头边界竖线（贯穿字幕区，帮助判断归属）----
        line_bottom = height - 24
        for shot in active:
            for frame in (shot.start_frame, shot.end_frame):
                cv.draw_line(x_of(frame), y - axis_h + 2, x_of(frame), line_bottom, GRID, 1)

        # ---- 每条字幕一行（需求 5：行数随字幕条数增加）----
        cv.set_font(13)
        for idx, row in enumerate(rows):
            sub = row.subtitle
            ry = y + idx * row_h + 4
            head = f"{sub.sid} [{sub.start_frame},{sub.end_frame})"
            cv.draw_text(16, ry + 1, head, INK_MUTED)

            x0, x1 = x_of(sub.start_frame), x_of(sub.end_frame)
            # 零长度区间（无效输入）放大为最小 6px 可视标记
            bw = max(6, x1 - x0) if not row.valid_input else max(2, x1 - x0)

            if not row.valid_input:
                # 无效：灰底虚线框（无填充色状态含义）
                cv.stroke_rect(x0, ry, bw, bar_h, GRAY, 2)
                # 零长度区间在起点位置加一条灰色竖线强调
                cv.draw_line(x0, ry - 2, x0, ry + bar_h + 2, GRAY, 2)
                status_text = "无效"
                status_color = GRAY
            else:
                owner = row.owner
                if row.status == VALID:
                    cv.fill_rect(x0, ry, bw, bar_h, GREEN)
                    cv.stroke_rect(x0, ry, bw, bar_h, BORDER, 1)
                    status_text, status_color = VALID, GREEN
                elif row.status == OVERLAP and owner is not None:
                    cv.fill_rect(x0, ry, bw, bar_h, GREEN)
                    cv.stroke_rect(x0, ry, bw, bar_h, BORDER, 1)
                    # 重叠帧段用橙色斜纹标出（可能有多段）
                    for other, _n in row.overlaps:
                        ox0 = x_of(max(sub.start_frame, other.start_frame))
                        ox1 = x_of(min(sub.end_frame, other.end_frame))
                        if ox1 > ox0:
                            cv.fill_rect(ox0, ry, ox1 - ox0, bar_h, ORANGE)
                            cv.hatch_rect(ox0, ry, ox1 - ox0, bar_h, (120, 50, 20))
                    status_text, status_color = OVERLAP, ORANGE
                elif row.status == OUT_OF_BOUNDS and owner is not None:
                    # 镜头内段绿色，越界段红色
                    sx0, sx1 = x_of(owner.start_frame), x_of(owner.end_frame)
                    inner_lo, inner_hi = max(x0, sx0), min(x1, sx1)
                    if inner_hi > inner_lo:
                        cv.fill_rect(inner_lo, ry, inner_hi - inner_lo, bar_h, GREEN)
                    if x0 < inner_lo:
                        cv.fill_rect(x0, ry, inner_lo - x0, bar_h, RED)  # 早于起点
                    if x1 > inner_hi:
                        cv.fill_rect(inner_hi, ry, x1 - inner_hi, bar_h, RED)  # 晚于终点
                    cv.stroke_rect(x0, ry, bw, bar_h, BORDER, 1)
                    status_text, status_color = OUT_OF_BOUNDS, RED
                else:
                    cv.fill_rect(x0, ry, bw, bar_h, RED)
                    cv.stroke_rect(x0, ry, bw, bar_h, BORDER, 1)
                    status_text, status_color = OUT_OF_BOUNDS, RED

            # 行尾状态文字（第二编码，颜色不单独承担含义）
            cv.set_font(12, bold=True)
            cv.draw_text(margin_left + plot_w + 8, ry + 1, status_text, status_color)
            cv.set_font(13)

        # ---- 图脚说明 ----
        cv.set_font(12)
        cv.draw_text(
            margin_left, height - 18,
            "说明：越界红色段位于镜头边界之外；橙色斜纹段为同镜头字幕重叠帧；详细冲突见 CSV。",
            INK_MUTED,
        )

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv.save_png(out)
