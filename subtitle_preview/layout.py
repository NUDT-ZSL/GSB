"""字体解析、文本折行与文本框定位。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from .models import MAX_TEXT_LINES, InputError, Manifest, Rect, SubtitleSpec, TextStyle

# Pillow 自带字体在缺少系统字体时兜底，确保无网络依赖。
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def resolve_font_path(style: TextStyle, base_dir: Path) -> str | None:
    if style.font_path:
        candidate = Path(style.font_path)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if not candidate.is_file():
            raise InputError(f"清单指定的字体文件不存在：{candidate}")
        return os.fspath(candidate)
    for path in _CJK_FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


@lru_cache(maxsize=8)
def _load_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    if font_path is None:
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(font_path, size=size)


def get_font(style: TextStyle, base_dir: Path) -> ImageFont.FreeTypeFont:
    return _load_font(resolve_font_path(style, base_dir), style.font_size)


@dataclass(frozen=True)
class TextLine:
    text: str
    width: int


@dataclass(frozen=True)
class TextMetrics:
    lines: tuple[TextLine, ...]
    line_height: int
    line_step: int
    box_width: int
    box_height: int


_TOKEN_RE = re.compile(r"\n|[A-Za-z0-9]+(?:['’\-._:/][A-Za-z0-9]+)*|\s+|.", re.DOTALL)


def _tokenize(text: str) -> list[str]:
    """把文本切成折行单元：拉丁词整体保留，CJK 与标点逐字成单元。"""
    return [match.group(0) for match in _TOKEN_RE.finditer(text)]


def _measure(font: ImageFont.FreeTypeFont, text: str, stroke_width: int) -> int:
    if not text:
        return 0
    bbox = font.getbbox(text, stroke_width=stroke_width)
    return int(bbox[2] - bbox[0])


def _wrap_paragraph(
    tokens: list[str], font: ImageFont.FreeTypeFont, stroke_width: int, max_width: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in tokens:
        if token == "\n":
            lines.append(current)
            current = ""
            continue
        if token.isspace():
            if current:
                pending = current + token
                if _measure(font, pending.rstrip(), stroke_width) <= max_width:
                    current = pending
            continue
        candidate = current + token
        if _measure(font, candidate.strip(), stroke_width) <= max_width:
            current = candidate
            continue
        if current.strip():
            lines.append(current.rstrip())
        current = ""
        if _measure(font, token, stroke_width) <= max_width:
            current = token
            continue
        # 单个单元超宽（长串无空格），按字符切分。
        for char in token:
            if _measure(font, (current + char).strip(), stroke_width) <= max_width:
                current += char
            else:
                if current.strip():
                    lines.append(current.rstrip())
                current = char
    if current.strip() or not lines:
        lines.append(current.rstrip())
    return lines


def measure_text(
    text: str, font: ImageFont.FreeTypeFont, style: TextStyle, max_width: int
) -> TextMetrics:
    if max_width <= 0:
        raise InputError("可用文本宽度不足")
    paragraphs = text.split("\n")
    wrapped: list[str] = []
    for paragraph in paragraphs:
        wrapped.extend(
            _wrap_paragraph(
                _tokenize(paragraph), font, style.stroke_width, max_width
            )
        )
    wrapped = [line for line in wrapped if line.strip()]
    if not wrapped:
        raise InputError("字幕文本为空白")

    ascent, descent = font.getmetrics()
    line_height = int(ascent + descent)
    line_step = int(round(style.font_size * style.line_spacing))
    line_step = max(line_step, line_height)
    content_width = max(_measure(font, line, style.stroke_width) for line in wrapped)
    content_height = (len(wrapped) - 1) * line_step + line_height
    box_width = content_width + 2 * style.padding
    box_height = content_height + 2 * style.padding
    return TextMetrics(
        lines=tuple(TextLine(line, _measure(font, line, style.stroke_width)) for line in wrapped),
        line_height=line_height,
        line_step=line_step,
        box_width=box_width,
        box_height=box_height,
    )


def usable_region(safe_area: Rect, panel: Rect | None) -> Rect | None:
    """安全区减去面板后的可用矩形。

    面板位于右侧时取面板左侧的竖带（与“水平居中于面板左侧可用区域”一致）；
    面板不在右侧时，取剩余分区中面积最大的一块。
    """
    if panel is None:
        return safe_area
    if not safe_area.intersects(panel):
        return safe_area
    left_band = (
        Rect(safe_area.left, safe_area.top, min(panel.left, safe_area.right), safe_area.bottom)
        if panel.left > safe_area.left
        else None
    )
    if left_band is not None and left_band.width > 0:
        return left_band
    candidates: list[Rect] = []
    if panel.top > safe_area.top:
        candidates.append(
            Rect(safe_area.left, safe_area.top, safe_area.right, min(panel.top, safe_area.bottom))
        )
    if panel.bottom < safe_area.bottom:
        candidates.append(
            Rect(safe_area.left, max(panel.bottom, safe_area.top), safe_area.right, safe_area.bottom)
        )
    viable = [rect for rect in candidates if rect.width > 0 and rect.height > 0]
    if not viable:
        return None
    return max(viable, key=lambda rect: (rect.width * rect.height, -rect.top, -rect.left))


def two_line_capacity_height(
    font: ImageFont.FreeTypeFont, style: TextStyle
) -> int:
    """安全区为容纳两行文本所需的文本框高度。"""
    ascent, descent = font.getmetrics()
    line_height = int(ascent + descent)
    line_step = max(int(round(style.font_size * style.line_spacing)), line_height)
    return line_height + line_step + 2 * style.padding


@dataclass(frozen=True)
class Placement:
    subtitle: SubtitleSpec
    placed: bool
    box: Rect | None
    frame_times: tuple[int, ...]
    result: str
    reason: str | None
    onset_offset_ms: int | None
    onset_offset_large: bool
    metrics: TextMetrics | None = None


def _unplaced(
    subtitle: SubtitleSpec,
    frame_times: tuple[int, ...],
    reason: str,
    offset: int | None,
    offset_large: bool,
) -> Placement:
    return Placement(
        subtitle, False, None, frame_times, "未放置", reason, offset, offset_large
    )


def plan_subtitle(
    manifest: Manifest,
    subtitle: SubtitleSpec,
    font: ImageFont.FreeTypeFont,
) -> Placement:
    style = manifest.style
    frame_times = tuple(
        frame.time_ms
        for frame in manifest.frames
        if subtitle.start_ms <= frame.time_ms <= subtitle.end_ms
    )
    offset = (
        None if subtitle.onset_ms is None else subtitle.start_ms - subtitle.onset_ms
    )
    offset_large = (
        offset is not None and abs(offset) > style.onset_review_threshold_ms
    )

    region = usable_region(manifest.safe_area, manifest.panel)
    if region is None or region.width <= 0 or region.height <= 0:
        return _unplaced(
            subtitle, frame_times, "安全区减去面板后没有可用区域", offset, offset_large
        )

    required_height = two_line_capacity_height(font, style)
    if region.height < required_height:
        return _unplaced(
            subtitle,
            frame_times,
            f"安全区可用高度 {region.height}px 不足以容纳两行文本"
            f"（需要 {required_height}px），该字幕未放置",
            offset,
            offset_large,
        )

    max_text_width = region.width - 2 * style.padding
    if max_text_width <= 0:
        return _unplaced(
            subtitle,
            frame_times,
            f"可用宽度 {region.width}px 扣除左右留白 {2 * style.padding}px 后不足以容纳文本",
            offset,
            offset_large,
        )

    metrics = measure_text(subtitle.text, font, style, max_text_width)
    if len(metrics.lines) > MAX_TEXT_LINES:
        return _unplaced(
            subtitle,
            frame_times,
            f"文本需要 {len(metrics.lines)} 行，超过安全区可容纳的 {MAX_TEXT_LINES} 行",
            offset,
            offset_large,
        )
    if metrics.box_width > region.width:
        return _unplaced(
            subtitle,
            frame_times,
            f"文本框宽度 {metrics.box_width}px 超过可用宽度 {region.width}px",
            offset,
            offset_large,
        )
    if metrics.box_height > region.height:
        return _unplaced(
            subtitle,
            frame_times,
            f"安全区可用高度 {region.height}px 不足以容纳文本框 {metrics.box_height}px",
            offset,
            offset_large,
        )

    left = region.left + (region.width - metrics.box_width) // 2
    top = region.top + (region.height - metrics.box_height) // 2
    box = Rect(left, top, left + metrics.box_width, top + metrics.box_height)

    result = "偏移过大" if offset_large else "已放置"
    reason = (
        f"字幕起始较起音偏差 {offset:+d}ms，绝对值超过 {style.onset_review_threshold_ms}ms，请编辑复核"
        if offset_large
        else None
    )
    return Placement(
        subtitle,
        True,
        box,
        frame_times,
        result,
        reason,
        offset,
        offset_large,
        metrics,
    )
