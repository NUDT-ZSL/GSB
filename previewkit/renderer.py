"""Pillow 逐帧绘制。

画面分为三块：
    上部 85%  —— 镜头占位（不同镜头用不同底色的渐变与编号标签，空档为明确的黑场）
    下方字幕条 —— 该时刻应显示的字幕文本（可能有 0、1 或多条）
    顶部信息条 —— 帧号、时间码、所在镜头/空档，便于逐帧核对

同一条字幕跨镜头时，切点前后两帧读到的是同一 SubtitleIndex 结果，绘制逻辑
与镜头无关，因此不会在切点被打断为空帧。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .models import EnvironmentProblem, FrameSpec
from .planner import KIND_GAP

# 6 套低饱和底色，按镜头编号的稳定哈希取模分配，保证同一输入每次同一颜色。
_PALETTES = (
    (28, 42, 74),
    (58, 38, 66),
    (24, 58, 52),
    (66, 50, 30),
    (34, 52, 78),
    (58, 34, 44),
)
_GAP_BG = (0, 0, 0)
_TEXT = (240, 244, 250)
_SUBTITLE_BAND = (10, 10, 12)
_SUBTITLE_TEXT = (255, 255, 255)
_META_TEXT = (176, 186, 200)
_ACCENT = (96, 160, 255)

_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)


def find_font(explicit: Optional[str] = None) -> Path:
    """定位支持中文的字体文件。找不到就报环境错误，绝不静默画成方框。"""
    candidates = ([Path(explicit)] if explicit else []) + list(_FONT_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    hint = "、".join(str(p) for p in _FONT_CANDIDATES[:3])
    raise EnvironmentProblem(
        f"找不到可用的中文字体，请用 --font 指定一个 .ttf/.ttc 文件（已尝试：{hint} 等）"
    )


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError as exc:
        raise EnvironmentProblem(f"字体文件无法加载：{path}（{exc}）") from exc


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行；中文逐字折，含空格的行优先在空格处折。"""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
            continue
        # 尝试回退到最近的空格，避免把英文单词劈开
        cut = current.rfind(" ")
        if ch != " " and cut > 0 and len(current) - cut < 16:
            lines.append(current[:cut])
            current = current[cut + 1 :] + ch
        else:
            lines.append(current)
            current = ch
    lines.append(current)
    return lines


def _timecode(ms: int) -> str:
    minutes, rem = divmod(ms, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def stable_palette_index(shot_id: str, size: int = 6) -> int:
    digest = hashlib.sha256(shot_id.encode("utf-8")).digest()
    return digest[0] % size


class Renderer:
    def __init__(
        self,
        width: int,
        height: int,
        font_path: Path,
        show_meta: bool = True,
        show_subtitles: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.font_path = str(font_path)
        self.show_meta = show_meta
        self.show_subtitles = show_subtitles

        self.meta_h = max(24, height // 24) if show_meta else 0
        self.sub_h = max(0, (height * 22) // 100) if show_subtitles else 0
        self.stage_h = height - self.meta_h - self.sub_h
        self.margin = max(8, width // 60)
        self.sub_font_size = max(12, min(self.sub_h // 3, width // 14))
        self.meta_font_size = max(9, min(self.meta_h - 8, width // 60))
        self.label_font_size = max(12, min(self.stage_h // 8, width // 12))

        self.font_sub = _font(self.font_path, self.sub_font_size)
        self.font_meta = _font(self.font_path, self.meta_font_size)
        self.font_label = _font(self.font_path, self.label_font_size)
        self._scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # ------------------------------------------------------------------ 绘制
    def render(self, spec: FrameSpec, total_ms: int) -> Image.Image:
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        self._draw_stage(img, draw, spec)
        if self.show_subtitles:
            self._draw_subtitles(draw, spec)
        if self.show_meta:
            self._draw_meta(draw, spec, total_ms)
        return img

    def _draw_stage(self, img: Image.Image, draw: ImageDraw.ImageDraw, spec: FrameSpec) -> None:
        top, bottom = self.meta_h, self.meta_h + self.stage_h
        if spec.kind == KIND_GAP:
            # 空档：纯黑背景 + 描边 + 明确的占位文字，绝不延续相邻镜头
            draw.rectangle([0, top, self.width - 1, bottom - 1], fill=_GAP_BG)
            draw.rectangle([0, top, self.width - 1, bottom - 1], outline=(80, 80, 88), width=2)
            label_y = top + self.stage_h // 2 - self.label_font_size
            self._line_at(draw, label_y, "空档 / GAP", self.font_label, (76, 76, 84))
            self._line_at(
                draw,
                label_y + int(self.label_font_size * 1.5),
                f"{spec.segment_start_ms}–{spec.segment_end_ms} ms（无镜头）",
                self.font_meta,
                (58, 58, 66),
            )
            return

        base = _PALETTES[spec.palette_index % len(_PALETTES)]
        # 垂直渐变，颜色由镜头编号稳定决定 → 不同镜头一眼可分，同一镜头每帧一致
        for y in range(top, bottom):
            t = (y - top) / max(1, bottom - top - 1)
            color = tuple(int(c * (0.65 + 0.5 * t)) for c in base)
            draw.line([(0, y), (self.width, y)], fill=color)
        # 镜头编号牌（画在信息条下方，避免与帧号重叠）
        tag = f"SHOT {spec.shot_id}"
        pad = max(6, self.width // 100)
        bbox = draw.textbbox((0, 0), tag, font=self.font_label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.rectangle(
            [
                self.margin,
                top + self.margin,
                self.margin + tw + pad * 2,
                top + self.margin + th + pad * 2,
            ],
            fill=_ACCENT,
        )
        draw.text(
            (self.margin + pad, top + self.margin + pad - bbox[1]),
            tag,
            font=self.font_label,
            fill=(8, 16, 34),
        )
        # 画面中央的占位说明：这是合成占位，不是真实素材
        centre_y = top + self.stage_h // 2 - self.meta_font_size
        self._line_at(
            draw,
            centre_y,
            "镜头画面占位",
            self.font_meta,
            tuple(min(255, c + 70) for c in base),
        )
        self._line_at(
            draw,
            centre_y + int(self.meta_font_size * 1.8),
            f"{spec.shot_id} · {spec.segment_start_ms}–{spec.segment_end_ms} ms",
            self.font_meta,
            tuple(min(255, c + 40) for c in base),
        )
        # 底部进度条：本镜头已走过的比例，方便核对切点位置
        span = max(1, spec.segment_end_ms - spec.segment_start_ms)
        ratio = min(1.0, max(0.0, (spec.time_ms - spec.segment_start_ms) / span))
        bar_y = bottom - max(4, self.stage_h // 60)
        draw.rectangle([0, bar_y, self.width - 1, bottom - 1], fill=(0, 0, 0))
        draw.rectangle([0, bar_y, int(self.width * ratio), bottom - 1], fill=_ACCENT)

    def _draw_subtitles(self, draw: ImageDraw.ImageDraw, spec: FrameSpec) -> None:
        top = self.height - self.sub_h
        draw.rectangle([0, top, self.width - 1, self.height - 1], fill=_SUBTITLE_BAND)
        texts = spec.subtitle_texts
        if not texts:
            note = "（本帧无字幕）"
            self._line_at(
                draw,
                top + (self.sub_h - self.meta_font_size) // 2,
                note,
                self.font_meta,
                (84, 88, 96),
            )
            return
        max_width = self.width - self.margin * 2
        line_h = int(self.sub_font_size * 1.32)
        lines: list[str] = []
        for text in texts:
            lines.extend(_wrap(self._scratch, text, self.font_sub, max_width))
        total_h = line_h * len(lines)
        # 字幕过多时整体缩小，保证不越界
        shrink = 1.0
        if total_h > self.sub_h - self.margin:
            shrink = max(0.45, (self.sub_h - self.margin) / total_h)
        font = (
            _font(self.font_path, max(11, int(self.sub_font_size * shrink)))
            if shrink < 1.0
            else self.font_sub
        )
        line_h = int(max(11, int(self.sub_font_size * shrink)) * 1.32)
        total_h = line_h * len(lines)
        y = top + max(self.margin // 3, (self.sub_h - total_h) // 2)
        for line in lines:
            w = draw.textlength(line, font=font)
            x = (self.width - w) / 2
            # 细描边，保证任何底色上都可读
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=_SUBTITLE_TEXT)
            y += line_h
        draw.line([(0, top), (self.width, top)], fill=_ACCENT, width=2)

    def _draw_meta(self, draw: ImageDraw.ImageDraw, spec: FrameSpec, total_ms: int) -> None:
        draw.rectangle([0, 0, self.width - 1, self.meta_h - 1], fill=(14, 14, 16))
        seg = (
            "GAP"
            if spec.kind == KIND_GAP
            else f"shot {spec.shot_id} [{spec.segment_start_ms}–{spec.segment_end_ms}ms]"
        )
        left = f"#{spec.index:05d}  {_timecode(spec.time_ms)}  {seg}"
        right = f"t={spec.time_ms}ms / {total_ms}ms"
        draw.text((self.margin, 3), left, font=self.font_meta, fill=_META_TEXT)
        rw = draw.textlength(right, font=self.font_meta)
        draw.text((self.width - self.margin - rw, 3), right, font=self.font_meta, fill=_META_TEXT)
        draw.line([(0, self.meta_h - 1), (self.width, self.meta_h - 1)], fill=(48, 48, 54), width=1)

    def _line_at(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        text: str,
        font,
        fill,
    ) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((self.width - w) / 2 - bbox[0], y - bbox[1]), text, font=font, fill=fill)
