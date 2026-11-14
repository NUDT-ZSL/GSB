"""文本排版与代表帧 PNG 渲染(仅依赖 Pillow,不做任何媒体解码)。"""
from __future__ import annotations

import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .parsers import Canvas

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",    # 微软雅黑(支持中文)
    r"C:\Windows\Fonts\simhei.ttf",  # 黑体
    r"C:\Windows\Fonts\simsun.ttc",  # 宋体
    r"C:\Windows\Fonts\arial.ttf",
]


def load_font(canvas: Canvas, size: Optional[int] = None) -> ImageFont.ImageFont:
    """按画幅配置加载字体;找不到 TTF 时退回 Pillow 内置点阵字体。"""
    size = size or canvas.font_size
    candidates = ([canvas.font_path] if canvas.font_path else []) + _FONT_CANDIDATES
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def text_width(font: ImageFont.ImageFont, s: str) -> float:
    return font.getlength(s) if s else 0.0


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: float) -> list[str]:
    """按像素宽度贪心换行(逐字符,兼容中英文);保留源文本中的显式换行。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            if cur and text_width(font, cur + ch) > max_width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines


def render_preview(
    canvas: Canvas,
    font: ImageFont.ImageFont,
    lines: list[str],
    out_path: str,
    violation: bool,
    label: str = "",
) -> None:
    """渲染一张与画幅同尺寸的代表帧:深灰底、安全区框、底部居中的字幕文本。"""
    img = Image.new("RGB", (canvas.width, canvas.height), (32, 32, 36))
    draw = ImageDraw.Draw(img)
    mx, my = canvas.margin_x, canvas.margin_y
    color = (220, 80, 80) if violation else (90, 200, 120)
    draw.rectangle([mx, my, canvas.width - mx - 1, canvas.height - my - 1],
                   outline=color, width=2)
    y = canvas.height - my - len(lines) * canvas.line_height
    for line in lines:
        w = text_width(font, line)
        x = (canvas.width - w) / 2
        draw.text((x, y), line, font=font, fill=(245, 245, 245),
                  stroke_width=2, stroke_fill=(0, 0, 0))
        y += canvas.line_height
    if label:
        small = load_font(canvas, size=max(14, canvas.font_size // 2))
        draw.text((mx + 6, my + 4), label, font=small, fill=(160, 160, 160))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG")
