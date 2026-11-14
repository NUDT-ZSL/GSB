"""切换预览图生成（需求 5、9）。

每个已对齐镜头输出一张 1280x720 PNG，画面包含：

* 镜头序号与标题（大号中文字体，自动回退到 Windows / 通用中文字体）；
* 对齐后起点时码 ``HH:MM:SS.mmm``；
* 原始起点、偏移量与偏移方向、所用鼓点时间；
* 节奏时间轴：全部鼓点刻度 + 原始起点 / 对齐后起点对照，便于人工复核。

文件名按镜头顺序零填充编号，资源管理器中即按切换顺序排列。
"""

from __future__ import annotations

import os
import re

from PIL import Image, ImageDraw, ImageFont

from .align import DIRECTION_LABELS, AlignedShot
from .detect import Beat
from .errors import OutputNotWritableError

WIDTH, HEIGHT = 1280, 720
MARGIN = 64
BG = (24, 28, 38)
PANEL = (36, 42, 56)
TEXT = (235, 238, 245)
MUTED = (158, 168, 188)
ACCENT = (88, 166, 255)
ORIGIN = (240, 180, 90)
GOOD = (106, 206, 128)
GRID = (72, 82, 102)

#: Windows / macOS / 常见 Linux 发行版自带中文字体，按优先级回退
_CANDIDATE_FONTS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def format_timecode(time_ms: int) -> str:
    """毫秒整数转 ``HH:MM:SS.mmm`` 时码。"""
    if time_ms < 0:
        return "-" + format_timecode(-time_ms)
    hours, rem = divmod(time_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _CANDIDATE_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def ensure_output_dir(path: str) -> None:
    """确保预览目录存在且可写，不可写时抛出可读错误（需求 9）。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise OutputNotWritableError(
            f"预览图输出目录不可写或无法创建：{path}（{exc}）。核查结果 JSON 已在写出预览前落盘，可正常打开。"
        ) from exc
    if not os.path.isdir(path):
        raise OutputNotWritableError(
            f"预览图输出目录不可写：{path} 已存在但不是目录。核查结果 JSON 已在写出预览前落盘，可正常打开。"
        )
    probe = os.path.join(path, ".write_test.tmp")
    try:
        with open(probe, "wb") as handle:
            handle.write(b"x")
    except OSError as exc:
        raise OutputNotWritableError(
            f"预览图输出目录不可写：{path}（{exc}）。核查结果 JSON 已在写出预览前落盘，可正常打开。"
        ) from exc
    else:
        try:
            os.remove(probe)
        except OSError:
            pass


_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(index: int, title: str) -> str:
    cleaned = _INVALID_FS_CHARS.sub("_", title).strip().strip(".")
    cleaned = cleaned[:40] or "shot"
    return f"shot_{index:03d}_{cleaned}.png"


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def render_preview(
    shot: AlignedShot,
    beats: list[Beat],
    audio_duration_ms: int,
    output_dir: str,
) -> str:
    """为单个已对齐镜头绘制预览图，返回图片绝对路径。"""
    font_title = _load_font(44)
    font_big = _load_font(64)
    font_body = _load_font(28)
    font_small = _load_font(22)

    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    # --- 顶部信息面板 ------------------------------------------------------
    draw.rounded_rectangle(
        (MARGIN, 40, WIDTH - MARGIN, 250), radius=18, fill=PANEL
    )
    draw.text((MARGIN + 32, 64), f"镜头 {shot.entry.index:02d}", font=font_small, fill=ACCENT)
    draw.text((MARGIN + 32, 100), shot.entry.title, font=font_title, fill=TEXT)

    tc_label = "对齐后起点时码"
    timecode = format_timecode(shot.aligned_start_ms)
    draw.text((MARGIN + 32, 170), tc_label, font=font_small, fill=MUTED)
    draw.text((MARGIN + 200, 158), timecode, font=font_big, fill=GOOD)

    # --- 中部数值核查 ------------------------------------------------------
    direction_label = DIRECTION_LABELS[shot.direction]
    lines = [
        f"原始起点：{shot.original_start_ms} ms（{format_timecode(shot.original_start_ms)}）",
        f"对齐后起点：{shot.aligned_start_ms} ms（{format_timecode(shot.aligned_start_ms)}）",
        f"偏移量：{shot.offset_ms} ms    偏移方向：{direction_label}",
        f"所用鼓点：{shot.beat_time_ms} ms（{format_timecode(shot.beat_time_ms)}）",
        f"镜头时长：{shot.entry.duration_ms} ms",
    ]
    y = 286
    for line in lines:
        draw.text((MARGIN + 8, y), line, font=font_body, fill=TEXT)
        y += 40

    # --- 底部节奏时间轴 ----------------------------------------------------
    axis_top = 580
    axis_bottom = 640
    label_row_upper = axis_top - 78
    label_row_lower = axis_top - 44
    axis_left = MARGIN + 8
    axis_right = WIDTH - MARGIN - 8
    draw.line((axis_left, axis_top, axis_left, axis_bottom), fill=GRID, width=2)
    draw.line((axis_left, axis_bottom, axis_right, axis_bottom), fill=GRID, width=2)
    span = max(audio_duration_ms, 1)

    def x_of(t_ms: int) -> float:
        return axis_left + min(t_ms / span, 1.0) * (axis_right - axis_left)

    for beat in beats:
        x = x_of(beat.time_ms)
        draw.line((x, axis_top, x, axis_bottom), fill=GRID, width=1)
        draw.ellipse((x - 4, axis_bottom - 8, x + 4, axis_bottom), fill=ACCENT)

    # 原始起点（橙色）与对齐后起点（绿色）对照；同点或过近时标签错位摆放
    x0 = x_of(shot.original_start_ms)
    draw.line((x0, axis_top - 14, x0, axis_bottom + 6), fill=ORIGIN, width=3)
    lx0 = min(max(x0 - 36, axis_left), axis_right - 72)
    draw.text((lx0, label_row_upper), "原始", font=font_small, fill=ORIGIN)

    x1 = x_of(shot.aligned_start_ms)
    draw.line((x1, axis_top - 14, x1, axis_bottom + 6), fill=GOOD, width=3)
    if abs(x1 - x0) < 80:
        # 重合或过近：对齐标签放到下排，避免互相遮挡
        lx1 = min(max(x1 + 10, axis_left), axis_right - 72)
        draw.text((lx1, label_row_lower), "对齐", font=font_small, fill=GOOD)
    else:
        lx1 = min(max(x1 - 36, axis_left), axis_right - 72)
        draw.text((lx1, label_row_upper), "对齐", font=font_small, fill=GOOD)

    legend = f"蓝点=鼓点（共 {len(beats)} 个）  橙线=原始起点  绿线=对齐后起点  配乐总长 {audio_duration_ms} ms"
    draw.text((axis_left, 672), legend, font=font_small, fill=MUTED)

    filename = _safe_filename(shot.entry.index, shot.entry.title)
    out_path = os.path.join(output_dir, filename)
    image.save(out_path, format="PNG")
    return os.path.abspath(out_path)


def render_all(
    aligned_shots: list[AlignedShot],
    beats: list[Beat],
    audio_duration_ms: int,
    output_dir: str,
) -> list[str]:
    """写出全部预览图；开始前统一做目录可写性预检（需求 9）。"""
    ensure_output_dir(output_dir)
    paths: list[str] = []
    for shot in aligned_shots:
        paths.append(render_preview(shot, beats, audio_duration_ms, output_dir))
    return paths
