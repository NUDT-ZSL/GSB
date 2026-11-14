"""画面合成：全尺寸标注图与候选帧横向拼贴对比预览。

每个视频输出一张与画布等宽的对比预览图：
- 同视频候选帧横向拼贴为若干格，格数 = 候选数；
- 超出画布宽时按比例缩格适配，总宽恒等于画布宽；
- 缺失/解码失败的候选保留占位格并写明原因；
- 每格均叠加保护框、文案排版结果与候选序号。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .layout import MODE_FALLBACK, LayoutResult, _wrap_text
from .models import Candidate, Rect, VideoEntry

_PREVIEW_SUFFIX = "_preview.png"
_BAR_BG = (232, 232, 232)
_BAR_BG_FALLBACK = (251, 238, 215)
_BAR_BORDER = (90, 90, 90)
_PLACEHOLDER_BG = (154, 154, 154)
_DARK_BG = (30, 30, 30)
_BOX_COLOR = (230, 40, 40)
_TEXT_FILL = (255, 255, 255)
_TEXT_STROKE = (20, 20, 20)
_BAR_TEXT = (40, 40, 40)


def preview_filename(video_id: str) -> str:
    """预览图固定命名规则：视频ID + 固定后缀。"""
    return f"{video_id}{_PREVIEW_SUFFIX}"


def probe_candidate(candidate: Candidate) -> tuple[Image.Image | None, str]:
    """尝试解码候选帧，返回 (RGB 图像, 原因)；不可用时图像为 None。"""
    if not candidate.path.is_file():
        candidate.available = False
        candidate.reason = "文件缺失"
        return None, candidate.reason
    try:
        # verify 做一次完整结构校验，再重新打开加载像素
        with Image.open(candidate.path) as probe:
            probe.verify()
        img = Image.open(candidate.path)
        img.load()
        if img.width <= 0 or img.height <= 0:
            raise ValueError("图像尺寸为 0")
        return img.convert("RGB"), ""
    except Exception as exc:  # Pillow 对损坏文件抛出的各类异常统一归类
        candidate.available = False
        candidate.reason = f"解码失败：{type(exc).__name__}"
        return None, candidate.reason


def _font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path), max(8, size))


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: Rect,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    """在指定矩形内居中绘制自动折行的说明文字。"""
    probe = draw
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for ch in paragraph:
            if probe.textlength(current + ch, font=font) <= box.width or not current:
                current += ch
            else:
                lines.append(current)
                current = ch
        lines.append(current)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    total_h = len(lines) * line_h
    y = box.y + max(0, (box.height - total_h) // 2)
    for line in lines:
        w = probe.textlength(line, font=font)
        x = box.x + max(0, (box.width - w) // 2)
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h


def _draw_placeholder(
    img: Image.Image, area: Rect, reason: str, raw_path: str, font_path: Path
) -> None:
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [area.x, area.y, area.right - 1, area.bottom - 1], fill=_PLACEHOLDER_BG
    )
    label_font = _font(font_path, max(16, area.width // 18))
    small_font = _font(font_path, max(12, area.width // 30))
    title_box = Rect(area.x + 8, area.y + area.height // 4, area.width - 16, label_font.size * 2)
    _draw_wrapped(draw, "候选帧不可用", title_box, label_font, (40, 40, 40))
    reason_box = Rect(
        area.x + 8,
        area.y + area.height // 4 + label_font.size * 2 + 8,
        area.width - 16,
        area.height // 3,
    )
    _draw_wrapped(draw, f"{reason}\n{raw_path}", reason_box, small_font, (40, 40, 40))


def render_overlay(
    entry: VideoEntry,
    frame: Image.Image | None,
    reason: str,
    raw_path: str,
    layout: LayoutResult,
    font_path: Path,
) -> Image.Image:
    """渲染一张画布全尺寸底图（帧/占位 + 保护框 + 预留条）。

    文案不在此处绘制：对比格宽度可能远小于画布，需在缩格内按格宽重排，
    由 ``_draw_cell_text`` 统一完成。
    """
    cw, ch = entry.canvas_width, entry.canvas_height
    bar = Rect(0, ch - entry.bar_height, cw, entry.bar_height)
    frame_area = Rect(0, 0, cw, ch - entry.bar_height)

    canvas = Image.new("RGB", (cw, ch), _DARK_BG)

    # 候选帧：等比缩放到画面区并居中；不可用则占位
    if frame is not None:
        fitted = ImageOps.contain(frame, (frame_area.width, frame_area.height))
        offset = (
            frame_area.x + (frame_area.width - fitted.width) // 2,
            frame_area.y + (frame_area.height - fitted.height) // 2,
        )
        canvas.paste(fitted, offset)
    else:
        _draw_placeholder(canvas, frame_area, reason, raw_path, font_path)

    draw = ImageDraw.Draw(canvas)

    # 主体保护框（红框）
    if entry.protection_box is not None:
        box = entry.protection_box
        line_w = max(2, cw // 200)
        draw.rectangle(
            [box.x, box.y, box.right - 1, box.bottom - 1],
            outline=_BOX_COLOR,
            width=line_w,
        )

    # 下方预留条
    bar_bg = _BAR_BG_FALLBACK if layout.mode == MODE_FALLBACK else _BAR_BG
    draw.rectangle([bar.x, bar.y, bar.right - 1, bar.bottom - 1], fill=bar_bg)
    draw.line(
        [(bar.x, bar.y), (bar.right - 1, bar.y)], fill=_BAR_BORDER, width=max(2, cw // 250)
    )
    return canvas


def _scale_rect(rect: Rect, sx: float, sy: float) -> Rect:
    return Rect(
        round(rect.x * sx),
        round(rect.y * sy),
        round(rect.width * sx),
        round(rect.height * sy),
    )


def _draw_cell_text(
    cell: Image.Image,
    entry: VideoEntry,
    layout: LayoutResult,
    font_path: Path,
    sx: float,
    sy: float,
) -> None:
    """在缩格内按格宽重新排版文案，保证不被格边界裁切。

    字号随缩格比例缩放（保留与全尺寸一致的避让/回退决策与最小字号比例），
    折行宽度按当前格宽重新计算。
    """
    draw = ImageDraw.Draw(cell)
    cell_size = max(8, round(layout.font_size * sx))
    font = _font(font_path, cell_size)
    margin = max(4, round(entry.canvas_width * 0.04 * sx))

    # 视频ID标注（条带左下角，不参与避让计算）
    id_font = _font(font_path, max(8, round(24 * sx)))
    if layout.mode == MODE_FALLBACK and layout.bar_rect is not None:
        bar = _scale_rect(layout.bar_rect, sx, sy)
        draw.text(
            (max(3, round(12 * sx)), bar.bottom - id_font.size - max(2, round(8 * sy))),
            entry.video_id,
            font=id_font,
            fill=_BAR_TEXT,
        )
    else:
        draw.text(
            (max(3, round(12 * sx)), cell.height - id_font.size - max(2, round(10 * sy))),
            entry.video_id,
            font=id_font,
            fill=(220, 220, 220),
        )

    if layout.mode == MODE_FALLBACK and layout.bar_rect is not None:
        bar = _scale_rect(layout.bar_rect, sx, sy)
        lines = _wrap_text(entry.copy_text, font, bar.width - 2 * margin)
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        leading = round(cell_size * 0.18)
        block_h = len(lines) * line_h + max(0, len(lines) - 1) * leading
        probe = draw
        block_w = max((probe.textlength(ln, font=font) for ln in lines), default=0)
        ox = bar.x + max(margin, round((bar.width - block_w) / 2))
        oy = bar.y + max(margin, round((bar.height - block_h) / 2))
        for ln in lines:
            if ln:
                draw.text((ox, oy), ln, font=font, fill=_BAR_TEXT)
            oy += line_h + leading
        return

    # 常规排版：以格内安全宽度重新折行，并按缩放后的保护框做避让，
    # 与全尺寸引擎保持同一套“缩字号直到不相交”的规则
    max_w = cell.width - 2 * margin
    cell_box = (
        _scale_rect(entry.protection_box, sx, sy)
        if entry.protection_box is not None
        else None
    )
    frame_bottom = round((entry.canvas_height - entry.bar_height) * sy)
    min_cell_size = max(8, round(entry.min_font_size * sx))
    size = cell_size
    while size >= min_cell_size:
        font = _font(font_path, size)
        lines = _wrap_text(entry.copy_text, font, max_w)
        ascent, descent = font.getmetrics()
        leading = round(size * 0.18)
        line_h = ascent + descent + leading
        text_bottom = margin + len(lines) * line_h - leading
        # 文案包围盒（保守取整行宽）
        text_rect = Rect(margin, margin, max_w, max(1, text_bottom - margin))
        collides = cell_box is not None and text_rect.intersects(cell_box)
        if not collides and text_bottom <= frame_bottom:
            break
        size = max(min_cell_size, size - max(1, round(2 * sx)))
        if size == min_cell_size and (collides or text_bottom > frame_bottom):
            break
    font = _font(font_path, size)
    lines = _wrap_text(entry.copy_text, font, max_w)
    ascent, descent = font.getmetrics()
    leading = round(size * 0.18)

    stroke_w = max(1, size // 12)
    y = margin
    for ln in lines:
        if y + ascent > frame_bottom:
            break
        if ln:
            draw.text(
                (margin, y),
                ln,
                font=font,
                fill=_TEXT_FILL,
                stroke_width=stroke_w,
                stroke_fill=_TEXT_STROKE,
            )
        y += ascent + descent + leading


def render_preview(
    entry: VideoEntry,
    decoded: list[tuple[Image.Image | None, str]],
    layout: LayoutResult,
    font_path: Path,
) -> Image.Image:
    """把各候选的全尺寸标注图横向拼贴为对比预览图。"""
    n = len(entry.candidates)
    cell_w = entry.canvas_width // n
    cell_h = entry.canvas_height // n
    sx, sy = cell_w / entry.canvas_width, cell_h / entry.canvas_height
    preview = Image.new("RGB", (entry.canvas_width, cell_h), _DARK_BG)

    badge_font = _font(font_path, max(10, round(40 * sx)))
    tag_font = _font(font_path, max(11, round(30 * sx)))

    for idx, ((frame, reason), candidate) in enumerate(zip(decoded, entry.candidates)):
        overlay = render_overlay(
            entry, frame, reason, candidate.raw_path, layout, font_path
        )
        cell = overlay.resize((cell_w, cell_h), Image.LANCZOS)

        # 用格内重排版覆盖全尺寸文案（后者按整画布宽度排版，会被格边界裁切）
        _draw_cell_text(cell, entry, layout, font_path, sx, sy)
        cell_draw = ImageDraw.Draw(cell)

        # 左上角候选序号徽标（尺寸随格缩放）
        badge = str(idx + 1)
        bw = cell_draw.textlength(badge, font=badge_font)
        pad = max(2, round(8 * sx))
        cell_draw.rectangle(
            [4, 4, 4 + bw + pad * 2, 4 + badge_font.size + pad * 2],
            fill=(20, 60, 120),
        )
        cell_draw.text((4 + pad, 4 + pad), badge, font=badge_font, fill=(255, 255, 255))

        # 排版方式与最终字号标签放在预留条右上角（与结果 JSON 一致），
        # 不与画面内容、帧标签、文案争空间
        tag = f"{layout.mode} {layout.font_size}px"
        tw = cell_draw.textlength(tag, font=tag_font)
        if tw + pad * 2 + 8 > cell_w:
            tag = f"{layout.font_size}px"
            tw = cell_draw.textlength(tag, font=tag_font)
        tag_h = tag_font.size + pad * 2
        tag_y1 = cell.height - max(3, round(6 * sy))
        tag_y0 = tag_y1 - tag_h
        cell_draw.rectangle(
            [cell_w - tw - pad * 2 - 4, tag_y0, cell_w - 4, tag_y1],
            fill=(245, 245, 245),
            outline=(120, 120, 120),
        )
        cell_draw.text(
            (cell_w - tw - pad - 4, tag_y0 + pad), tag, font=tag_font, fill=(30, 30, 30)
        )

        preview.paste(cell, (idx * cell_w, 0))
    return preview
