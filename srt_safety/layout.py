"""按安全区宽度与字号换行，并记录每行实际像素宽度。

换行规则（确定性，不依赖词典/分词库）：
- SRT 显式换行（\\n）始终保留为硬换行；
- 拉丁/数字等以空白分隔的"单词"为原子单位，不在单词内部断行；
- 中日韩等宽字符可逐字换行；
- 单个原子 token 本身就超过安全区宽度时如实超宽并标记（用于越界检测）。
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# 用于纯测量的共享绘图对象（不关联任何输出图片）
_MEASURE_IMG = Image.new("RGB", (4, 4))
_MEASURE_DRAW = ImageDraw.Draw(_MEASURE_IMG)


def _text_width(font: ImageFont.ImageFont, text: str) -> int:
    bbox = _MEASURE_DRAW.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


def _tokenize(paragraph: str) -> list[str]:
    """把段落切成原子 token：CJK 逐字、其它连续非空白字符作为整体单词。"""
    tokens: list[str] = []
    run = ""
    for char in paragraph:
        if char.isspace():
            if run:
                tokens.append(run)
                run = ""
            continue
        if _is_cjk(char):
            if run:
                tokens.append(run)
                run = ""
            tokens.append(char)
        else:
            run += char
    if run:
        tokens.append(run)
    return tokens


@dataclass(frozen=True)
class LayoutLine:
    text: str
    width_px: int


@dataclass(frozen=True)
class TextLayout:
    lines: list[LayoutLine]
    line_count: int
    max_width_px: int
    block_height_px: int
    exceeds_safe_width: bool


def wrap_text(text: str, font: ImageFont.ImageFont, safe_width: int,
              line_height: int) -> TextLayout:
    out_lines: list[LayoutLine] = []

    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        current = ""
        for token in _tokenize(paragraph):
            if not current:
                current = token
                continue
            candidate = current + token
            if _text_width(font, candidate) <= safe_width:
                current = candidate
            else:
                out_lines.append(LayoutLine(current, _text_width(font, current)))
                current = token
        out_lines.append(LayoutLine(current, _text_width(font, current)))

    max_width = max((line.width_px for line in out_lines), default=0)
    return TextLayout(
        lines=out_lines,
        line_count=len(out_lines),
        max_width_px=max_width,
        block_height_px=len(out_lines) * line_height,
        exceeds_safe_width=max_width > safe_width,
    )
