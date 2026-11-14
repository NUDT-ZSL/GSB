"""双语对齐预览图渲染（Pillow）。

布局自上而下：
1. 标题与图例；
2. 毫秒时间轴刻度；
3. 甘特式句子卡片：按配置顺序贪心分配泳道，卡片位置严格对应时间区间；
   中文行用蓝色、英文行用绿色标注行数；行数不匹配/超行/时间重叠的句
   用醒目红/橙/紫边框标出；
4. 无效句清单（不进入时间轴，仅列原因）；
5. 无有效句时整图为说明性占位图。

所有坐标均为整数且仅依赖输入数据，同输入重复运行逐字节一致。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw

from .audit import FLAG_LINE_MISMATCH, FLAG_OVER_LINES, FLAG_OVERLAP
from .textwrap import resolve_font, wrap_text

# 调色板（固定，保证可复现）
COLOR_BG = (250, 250, 252)
COLOR_TITLE = (32, 36, 44)
COLOR_TEXT = (48, 52, 60)
COLOR_MUTED = (110, 116, 128)
COLOR_AXIS = (150, 156, 168)
COLOR_GRID = (226, 229, 236)
COLOR_CARD_BG = (255, 255, 255)
COLOR_ZH = (24, 92, 190)       # 中文行：蓝
COLOR_EN = (24, 140, 84)       # 英文行：绿
COLOR_OVERLAP = (214, 48, 49)  # 时间重叠：红
COLOR_MISMATCH = (232, 132, 20)  # 行数不匹配：橙
COLOR_OVER_LINES = (140, 72, 200)  # 超行：紫
COLOR_INVALID_BG = (252, 240, 240)
COLOR_INVALID_BORDER = (214, 48, 49)
COLOR_PLACEHOLDER_BG = (245, 246, 250)

MARGIN_X = 24
TITLE_H = 64
LEGEND_H = 34
AXIS_H = 34
LANE_GAP = 12
CARD_MIN_W = 132
CARD_PAD = 10
BOTTOM_PAD = 20

# 卡片内部纵向分区：头部 / 中文最多2行(+折叠提示) / 间隔 / 英文最多2行(+折叠提示) / 页脚
def _lane_height(font_size: int) -> int:
    line_step = font_size + 5
    small_h = max(12, int(font_size * 0.62))
    block = line_step * 2 + small_h + 8  # 含折叠提示的单语块
    return 26 + block + 6 + block + 26


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """按像素宽度截断文本，超出部分以省略号表示。"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis if text else ellipsis


def _nice_tick_step(duration: int) -> int:
    """选择让刻度数在 4~10 之间的整齐步长。"""
    target_ticks = 8
    rough = max(1, duration // target_ticks)
    powers = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
              10000, 25000, 50000, 100000, 250000, 500000, 1000000]
    for step in powers:
        if step >= rough:
            return step
    return powers[-1]


def _card_border_color(flags: list[str]) -> tuple[int, int, int] | None:
    """边框优先级：重叠(红) > 不匹配(橙) > 超行(紫)。"""
    if FLAG_OVERLAP in flags:
        return COLOR_OVERLAP
    if FLAG_LINE_MISMATCH in flags:
        return COLOR_MISMATCH
    if FLAG_OVER_LINES in flags:
        return COLOR_OVER_LINES
    return None


def _draw_legend(draw: ImageDraw.ImageDraw, y: int, small_font) -> int:
    items = [
        (COLOR_ZH, "中文行"),
        (COLOR_EN, "英文行"),
        (COLOR_OVERLAP, "时间重叠边框"),
        (COLOR_MISMATCH, "行数不匹配边框"),
        (COLOR_OVER_LINES, "超行边框"),
    ]
    x = MARGIN_X
    for color, label in items:
        draw.rectangle([x, y + 4, x + 16, y + 20], fill=color)
        draw.text((x + 22, y + 3), label, fill=COLOR_TEXT, font=small_font)
        x += 22 + int(draw.textlength(label, font=small_font)) + 22
    return y + LEGEND_H


def _draw_placeholder(width: int, report: dict[str, Any], fonts: dict) -> Image.Image:
    """无有效句时的说明性占位图。"""
    height = 320
    img = Image.new("RGB", (width, height), COLOR_PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)
    title_font, small_font = fonts["title"], fonts["small"]

    draw.text((MARGIN_X, 28), "双语字幕对齐预览", fill=COLOR_TITLE, font=title_font)
    draw.rectangle(
        [MARGIN_X, 96, width - MARGIN_X, 220],
        fill=COLOR_INVALID_BG,
        outline=COLOR_INVALID_BORDER,
        width=2,
    )
    draw.text(
        (MARGIN_X + 16, 112),
        "无有效字幕：所有句子均无效，时间轴上没有可展示的字幕。",
        fill=COLOR_INVALID_BORDER,
        font=fonts["body"],
    )
    reasons = report["summary"].get("invalid_sentence_ids", [])
    line = "无效句 id：" + ("、".join(reasons) if reasons else "（字幕表为空）")
    draw.text(
        (MARGIN_X + 16, 150),
        _truncate(draw, line, small_font, width - 2 * MARGIN_X - 32),
        fill=COLOR_TEXT,
        font=small_font,
    )
    draw.text(
        (MARGIN_X + 16, 182),
        "请依据核对报告中的 invalid_reasons 返工后重新运行。",
        fill=COLOR_MUTED,
        font=small_font,
    )
    return img


def render_preview(report: dict[str, Any]) -> bytes:
    """根据核对报告渲染预览图，返回 PNG 字节。"""
    config = report["config"]
    width: int = config["canvas_width"]
    font_size: int = config["font_size"]
    duration: int = config["video_duration_ms"]

    title_font, title_src = resolve_font(int(font_size * 1.3))
    body_font, _ = resolve_font(font_size)
    small_font, _ = resolve_font(max(12, int(font_size * 0.62)))
    fonts = {"title": title_font, "body": body_font, "small": small_font}

    valid = [r for r in report["sentences"] if r["valid"]]
    if not valid:
        img = _draw_placeholder(width, report, fonts)
        return _to_png_bytes(img)

    # ---- 泳道贪心分配（按配置顺序，确定性） ----
    lane_ends: list[int] = []
    lane_of: dict[int, int] = {}
    for record in valid:
        placed = False
        for lane, end in enumerate(lane_ends):
            if record["start_ms"] >= end:
                lane_ends[lane] = record["end_ms"]
                lane_of[record["order"]] = lane
                placed = True
                break
        if not placed:
            lane_ends.append(record["end_ms"])
            lane_of[record["order"]] = len(lane_ends) - 1
    lane_count = len(lane_ends)

    lane_h = _lane_height(font_size)
    invalid = [r for r in report["sentences"] if not r["valid"]]
    invalid_h = 30 + len(invalid) * 24 if invalid else 0
    height = (
        TITLE_H
        + LEGEND_H
        + AXIS_H
        + lane_count * (lane_h + LANE_GAP)
        + invalid_h
        + BOTTOM_PAD
    )

    img = Image.new("RGB", (width, height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    draw.text((MARGIN_X, 18), "双语字幕对齐预览", fill=COLOR_TITLE, font=title_font)
    meta = f"画布 {width}px / 字号 {font_size}px / 字体 {title_src}"
    draw.text((width - MARGIN_X - int(draw.textlength(meta, font=small_font)), 24),
              meta, fill=COLOR_MUTED, font=small_font)
    legend_y = TITLE_H - 8
    _draw_legend(draw, legend_y, small_font)

    axis_y = TITLE_H + LEGEND_H
    timeline_left = MARGIN_X
    timeline_right = width - MARGIN_X
    timeline_w = timeline_right - timeline_left

    def x_of(ms: int) -> int:
        return timeline_left + round(ms * timeline_w / max(1, duration))

    # 时间轴与网格
    draw.line([timeline_left, axis_y + 22, timeline_right, axis_y + 22],
              fill=COLOR_AXIS, width=2)
    step = _nice_tick_step(duration)
    tick = 0
    while tick <= duration:
        x = x_of(tick)
        draw.line([x, axis_y + 18, x, axis_y + 26], fill=COLOR_AXIS, width=1)
        label = f"{tick}ms"
        label_w = int(draw.textlength(label, font=small_font))
        label_x = x - label_w // 2
        # 首尾标签向内收，避免被画布边缘裁切
        label_x = max(timeline_left, min(label_x, timeline_right - label_w))
        draw.text((label_x, axis_y + 2), label, fill=COLOR_MUTED, font=small_font)
        for lane in range(lane_count):
            gy = axis_y + AXIS_H + lane * (lane_h + LANE_GAP)
            draw.line([x, gy, x, gy + lane_h], fill=COLOR_GRID, width=1)
        tick += step

    # 句子卡片
    for record in valid:
        lane = lane_of[record["order"]]
        x0 = max(timeline_left, x_of(record["start_ms"]))
        x1 = min(timeline_right, x_of(record["end_ms"]))
        if x1 - x0 < CARD_MIN_W:
            x1 = min(timeline_right, x0 + CARD_MIN_W)
        y0 = axis_y + AXIS_H + lane * (lane_h + LANE_GAP)
        y1 = y0 + lane_h

        border = _card_border_color(record["flags"])
        draw.rectangle([x0, y0, x1, y1], fill=COLOR_CARD_BG,
                       outline=border or COLOR_GRID, width=3 if border else 1)

        inner_w = x1 - x0 - 2 * CARD_PAD
        header = f"{record['id']}  {record['start_ms']}-{record['end_ms']}ms"
        draw.text((x0 + CARD_PAD, y0 + 6),
                  _truncate(draw, header, small_font, inner_w),
                  fill=COLOR_MUTED, font=small_font)

        # 卡片宽度小于画布，这里仅做展示用截断；权威行数以报告
        # （按画布渲染宽度测量）的 zh_lines/en_lines 为准
        zh_preview = wrap_text(record["zh_text"], body_font, inner_w)
        en_preview = wrap_text(record["en_text"], body_font, inner_w)
        line_step = font_size + 5

        def draw_lang_block(preview_lines: list[str], total_lines: int,
                            color, top_y: int) -> int:
            """绘制单语块：最多展示 2 行，超出部分用报告行数给出折叠提示。"""
            for line in preview_lines[:2]:
                draw.text((x0 + CARD_PAD, top_y),
                          _truncate(draw, line, body_font, inner_w),
                          fill=color, font=body_font)
                top_y += line_step
            if total_lines > 2:
                draw.text((x0 + CARD_PAD, top_y + 2),
                          f"…报告计 {total_lines} 行",
                          fill=color, font=small_font)
                top_y += small_font.size + 6
            return top_y

        ty = y0 + 26
        ty = draw_lang_block(zh_preview, record["zh_lines"], COLOR_ZH, ty)
        ty += 6
        ty = draw_lang_block(en_preview, record["en_lines"], COLOR_EN, ty)

        footer = f"中{record['zh_lines']} 英{record['en_lines']}"
        if record["line_count_diff"]:
            footer += f" 差{record['line_count_diff']:+d}"
        if record["intra_overlap_ms"] is not None:
            footer += f" 叠{record['intra_overlap_ms']}ms"
        draw.text((x0 + CARD_PAD, y1 - 22),
                  _truncate(draw, footer, small_font, inner_w),
                  fill=border or COLOR_TEXT, font=small_font)

    # 无效句清单
    if invalid:
        top = axis_y + AXIS_H + lane_count * (lane_h + LANE_GAP) + 8
        draw.text((MARGIN_X, top), "无效句（未进入时间轴）：",
                  fill=COLOR_INVALID_BORDER, font=small_font)
        for k, record in enumerate(invalid):
            reason = "；".join(record["invalid_reasons"])
            line = f"{record['id']}: {reason}"
            draw.text(
                (MARGIN_X + 8, top + 24 + k * 24),
                _truncate(draw, line, small_font, width - 2 * MARGIN_X - 8),
                fill=COLOR_TEXT,
                font=small_font,
            )

    return _to_png_bytes(img)


def _to_png_bytes(img: Image.Image) -> bytes:
    """PNG 编码：固定参数，保证同输入字节级一致。"""
    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()
