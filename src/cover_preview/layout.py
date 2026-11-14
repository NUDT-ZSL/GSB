"""文案排版引擎。

规则：
1. 文案默认从画布左上角安全边距处逐行排版（支持手动换行、按宽度自动折行）。
2. 若文案包围盒与主体保护框相交，则字号递减 2px 重新排版，
   直到完全避让，或达到可辨识最小字号。
3. 达到最小字号仍相交时，将文案整体移入画布下方预留条，
   保留最小可辨识字号，标注为“回退排版”。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Rect, VideoEntry

MODE_INLINE = "常规排版"
MODE_FALLBACK = "回退排版"

# 排版安全边距（相对画布宽度的比例，下限 12px）
_MARGIN_RATIO = 0.04
# 字号递减步长
_SIZE_STEP = 2
# 行间距（相对字号）
_LEADING_RATIO = 0.18


@dataclass
class LayoutResult:
    """一次排版的完整结果。"""

    mode: str
    font_size: int
    lines: list[str]
    positions: list[tuple[int, int]]   # 每行左上角坐标
    line_boxes: list[Rect]
    union_box: Rect | None             # 全部文字的包围盒（无文案时为 None）
    bar_rect: Rect | None              # 下方预留条（仅回退时有内容约束意义）


def _load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path), size)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按可用宽度逐字符折行，保留显式换行符。"""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            width = probe.textlength(candidate, font=font)
            if width <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    return lines


def _measure(
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    origin_x: int,
    origin_y: int,
) -> tuple[list[tuple[int, int]], list[Rect], Rect | None]:
    """计算每行位置、每行包围盒与整体包围盒。"""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    leading = round(font.size * _LEADING_RATIO)

    positions: list[tuple[int, int]] = []
    boxes: list[Rect] = []
    cursor_y = origin_y
    for line in lines:
        positions.append((origin_x, cursor_y))
        if line:
            left, top, right, bottom = probe.textbbox(
                (origin_x, cursor_y), line, font=font
            )
            boxes.append(Rect(left, top, right - left, bottom - top))
        cursor_y += line_height + leading

    if not boxes:
        return positions, boxes, None
    union = Rect(
        x=min(b.x for b in boxes),
        y=min(b.y for b in boxes),
        width=max(b.right for b in boxes) - min(b.x for b in boxes),
        height=max(b.bottom for b in boxes) - min(b.y for b in boxes),
    )
    return positions, boxes, union


def _layout_in_bar(
    entry: VideoEntry, font_path: Path, size: int
) -> LayoutResult:
    """将文案在下方预留条内水平/垂直居中排版。"""
    font = _load_font(font_path, size)
    margin = max(12, round(entry.canvas_width * _MARGIN_RATIO))
    bar = Rect(
        0,
        entry.canvas_height - entry.bar_height,
        entry.canvas_width,
        entry.bar_height,
    )
    lines = _wrap_text(entry.copy_text, font, entry.canvas_width - 2 * margin)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    leading = round(size * _LEADING_RATIO)
    block_height = len(lines) * line_height + max(0, len(lines) - 1) * leading
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    block_width = max(
        (probe.textlength(line, font=font) for line in lines), default=0
    )
    origin_x = max(margin, round((entry.canvas_width - block_width) / 2))
    origin_y = bar.y + max(margin, round((bar.height - block_height) / 2))
    positions, boxes, union = _measure(lines, font, origin_x, origin_y)
    return LayoutResult(
        mode=MODE_FALLBACK,
        font_size=size,
        lines=lines,
        positions=positions,
        line_boxes=boxes,
        union_box=union,
        bar_rect=bar,
    )


def layout_copy(entry: VideoEntry, font_path: Path) -> LayoutResult:
    """按避让规则计算文案排版结果。"""
    margin = max(12, round(entry.canvas_width * _MARGIN_RATIO))
    max_text_width = entry.canvas_width - 2 * margin

    # 从最大字号向下尝试
    size = entry.max_font_size
    while size >= entry.min_font_size:
        font = _load_font(font_path, size)
        lines = _wrap_text(entry.copy_text, font, max_text_width)
        positions, boxes, union = _measure(lines, font, margin, margin)
        if entry.protection_box is None or union is None:
            collision = False
        else:
            collision = union.intersects(entry.protection_box)
        if not collision:
            return LayoutResult(
                mode=MODE_INLINE,
                font_size=size,
                lines=lines,
                positions=positions,
                line_boxes=boxes,
                union_box=union,
                bar_rect=None,
            )
        size -= _SIZE_STEP

    # 最小字号仍相交（或无保护框理论上不会走到这里）：回退到预留条
    return _layout_in_bar(entry, font_path, entry.min_font_size)
