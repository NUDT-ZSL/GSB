"""清单数据模型与解析。

程序只接受由清单驱动的输入：帧时间戳/PNG 路径、字幕起止毫秒/文本、
面板区域像素坐标。所有相对路径都相对于清单文件所在目录解析。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

# 安全区最多容纳的行数（需求规定为两行）。
MAX_TEXT_LINES = 2


class InputError(Exception):
    """输入清单、字体或参数不可用时抛出，对应非零退出码。"""


@dataclass(frozen=True)
class Rect:
    """整数像素矩形，边界为左闭右开 [left, right) / [top, bottom)。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def intersects(self, other: "Rect") -> bool:
        """仅边线接触不算相交。"""
        return (
            self.left < other.right
            and other.left < self.right
            and self.top < other.bottom
            and other.top < self.bottom
        )

    def contains(self, other: "Rect") -> bool:
        return (
            self.left <= other.left
            and other.right <= self.right
            and self.top <= other.top
            and other.bottom <= self.bottom
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }


@dataclass(frozen=True)
class TextStyle:
    font_size: int = 36
    line_spacing: float = 1.35
    padding: int = 16
    stroke_width: int = 2
    fill: tuple[int, int, int] = (255, 255, 255)
    stroke_fill: tuple[int, int, int] = (0, 0, 0)
    font_path: str | None = None
    onset_review_threshold_ms: int = 500


@dataclass(frozen=True)
class FrameSpec:
    index: int
    time_ms: int
    path_raw: str
    path: Path


@dataclass(frozen=True)
class SubtitleSpec:
    index: int  # 在输入字幕表中的原始下标
    start_ms: int
    end_ms: int
    text: str
    onset_ms: int | None


@dataclass(frozen=True)
class Manifest:
    path: Path
    base_dir: Path
    canvas: tuple[int, int]
    safe_area: Rect
    panel: Rect | None
    panel_status: str  # "ok" | "missing" | "invalid"
    frames: tuple[FrameSpec, ...]
    subtitles: tuple[SubtitleSpec, ...]
    style: TextStyle
    warnings: tuple[str, ...]


def _require_int(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"字段 {field_name} 必须是整数，实际为 {value!r}")
    if minimum is not None and value < minimum:
        raise InputError(f"字段 {field_name} 不能小于 {minimum}，实际为 {value}")
    return value


def _require_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"字段 {field_name} 必须是数字，实际为 {value!r}")
    return float(value)


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _parse_color(value: Any, field_name: str) -> tuple[int, int, int]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            try:
                return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
            except ValueError:
                pass
        raise InputError(f"字段 {field_name} 必须是 #RRGGBB 颜色，实际为 {value!r}")
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        channels = []
        for i, channel in enumerate(value[:3]):
            channel = _require_int(channel, f"{field_name}[{i}]", minimum=0)
            if channel > 255:
                raise InputError(f"字段 {field_name}[{i}] 必须在 0..255 之间")
            channels.append(channel)
        return tuple(channels)  # type: ignore[return-value]
    raise InputError(f"字段 {field_name} 必须是颜色字符串或 RGB 数组")


def _parse_rect_dict(raw: Any, field_name: str) -> Rect:
    if not isinstance(raw, dict):
        raise InputError(f"字段 {field_name} 必须是对象")
    if all(key in raw for key in ("left", "top", "right", "bottom")):
        left = _require_int(raw["left"], f"{field_name}.left")
        top = _require_int(raw["top"], f"{field_name}.top")
        right = _require_int(raw["right"], f"{field_name}.right")
        bottom = _require_int(raw["bottom"], f"{field_name}.bottom")
    elif all(key in raw for key in ("x", "y", "width", "height")):
        left = _require_int(raw["x"], f"{field_name}.x")
        top = _require_int(raw["y"], f"{field_name}.y")
        width = _require_int(raw["width"], f"{field_name}.width")
        height = _require_int(raw["height"], f"{field_name}.height")
        right = left + width
        bottom = top + height
    else:
        raise InputError(
            f"字段 {field_name} 需要 left/top/right/bottom 或 x/y/width/height"
        )
    return Rect(left, top, right, bottom)


def _default_safe_area(width: int, height: int) -> Rect:
    inset_x = max(8, round(width * 0.05))
    inset_y = max(8, round(height * 0.05))
    return Rect(inset_x, inset_y, width - inset_x, height - inset_y)


def _infer_canvas(frames: Iterable[FrameSpec], warnings: list[str]) -> tuple[int, int]:
    for frame in frames:
        try:
            with Image.open(frame.path) as image:
                size = image.size
        except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError):
            continue
        warnings.append(
            f"清单未提供画布尺寸，已从帧 {frame.path_raw} 推断为 {size[0]}x{size[1]}"
        )
        return size
    raise InputError("无法从帧文件推断画布尺寸，请在清单中提供 canvas")


def _parse_style(raw: Any) -> TextStyle:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InputError("字段 style 必须是对象")

    font_size = _require_int(raw.get("font_size", 36), "style.font_size", minimum=1)
    line_spacing = _require_number(raw.get("line_spacing", 1.35), "style.line_spacing")
    if not 1.0 <= line_spacing <= 4.0:
        raise InputError("字段 style.line_spacing 必须在 1.0..4.0 之间")
    padding = _require_int(raw.get("padding", 16), "style.padding", minimum=0)
    stroke_width = _require_int(
        raw.get("stroke_width", 2), "style.stroke_width", minimum=0
    )
    fill = _parse_color(raw.get("fill", "#FFFFFF"), "style.fill")
    stroke_fill = _parse_color(raw.get("stroke_fill", "#000000"), "style.stroke_fill")
    threshold = _require_int(
        raw.get("onset_review_threshold_ms", 500),
        "style.onset_review_threshold_ms",
        minimum=1,
    )
    font_path = raw.get("font_path")
    if font_path is not None and not isinstance(font_path, str):
        raise InputError("字段 style.font_path 必须是字符串或 null")
    return TextStyle(
        font_size=font_size,
        line_spacing=line_spacing,
        padding=padding,
        stroke_width=stroke_width,
        fill=fill,
        stroke_fill=stroke_fill,
        font_path=font_path,
        onset_review_threshold_ms=threshold,
    )


def _parse_panel(
    raw: Any, canvas_width: int, canvas_height: int, warnings: list[str]
) -> tuple[Rect | None, str]:
    if raw is None:
        warnings.append("清单未提供面板区域，字幕按整个安全区居中")
        return None, "missing"
    if not isinstance(raw, dict):
        warnings.append(f"面板区域格式非法（期望对象，实际为 {type(raw).__name__}），已按无面板处理")
        return None, "invalid"
    try:
        rect = _parse_rect_dict(raw, "panel")
    except InputError as exc:
        warnings.append(f"面板区域坐标非法（{exc}），已按无面板处理")
        return None, "invalid"
    if rect.width <= 0 or rect.height <= 0:
        warnings.append("面板区域坐标非法（宽度或高度不为正），已按无面板处理")
        return None, "invalid"
    if rect.left < 0 or rect.top < 0:
        warnings.append("面板区域坐标非法（存在负坐标），已按无面板处理")
        return None, "invalid"
    if rect.left >= canvas_width or rect.top >= canvas_height:
        warnings.append(
            f"面板区域坐标非法（起点超出画布 {canvas_width}x{canvas_height}），已按无面板处理"
        )
        return None, "invalid"
    if rect.right > canvas_width or rect.bottom > canvas_height:
        warnings.append(
            f"面板区域坐标非法（超出画布 {canvas_width}x{canvas_height}），已按无面板处理"
        )
        return None, "invalid"
    return rect, "ok"


def _parse_subtitles(raw: Any) -> tuple[list[SubtitleSpec], int]:
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        raise InputError("字段 subtitles 必须是数组")
    subtitles: list[SubtitleSpec] = []
    blank_count = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputError(f"subtitles[{index}] 必须是对象")
        start = _require_int(
            _first(item, "start_ms", "start", "startMs"), f"subtitles[{index}].start_ms"
        )
        end = _require_int(
            _first(item, "end_ms", "end", "endMs"), f"subtitles[{index}].end_ms"
        )
        if end < start:
            raise InputError(
                f"subtitles[{index}] 的 end_ms({end}) 小于 start_ms({start})"
            )
        text = _first(item, "text", "content")
        if not isinstance(text, str):
            raise InputError(f"subtitles[{index}].text 必须是字符串")
        if not text.strip():
            blank_count += 1
            continue
        onset_raw = _first(item, "onset_ms", "onset", "onsetMs")
        onset = (
            None
            if onset_raw is None
            else _require_int(onset_raw, f"subtitles[{index}].onset_ms")
        )
        subtitles.append(
            SubtitleSpec(
                index=index,
                start_ms=start,
                end_ms=end,
                text=text,
                onset_ms=onset,
            )
        )
    return subtitles, blank_count


def load_manifest(manifest_path: str | os.PathLike[str]) -> Manifest:
    path = Path(manifest_path)
    if not path.is_file():
        raise InputError(f"清单文件不存在：{path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"清单文件不是有效的 UTF-8：{exc}") from exc
    except OSError as exc:
        raise InputError(f"无法读取清单文件：{exc}") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InputError(f"清单 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise InputError("清单根节点必须是 JSON 对象")

    warnings: list[str] = []
    base_dir = path.parent

    raw_frames = data.get("frames")
    if not isinstance(raw_frames, list):
        raise InputError("清单必须包含 frames 数组")
    frames: list[FrameSpec] = []
    for index, item in enumerate(raw_frames):
        if not isinstance(item, dict):
            raise InputError(f"frames[{index}] 必须是对象")
        raw_path = _first(item, "path", "file", "png")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise InputError(f"frames[{index}] 缺少 PNG 路径（path）")
        time_raw = _first(item, "time_ms", "ms", "timestamp_ms", "timestamp")
        time_ms = _require_int(time_raw, f"frames[{index}].time_ms", minimum=0)
        resolved = Path(raw_path)
        if not resolved.is_absolute():
            resolved = base_dir / resolved
        frames.append(
            FrameSpec(
                index=index,
                time_ms=time_ms,
                path_raw=raw_path,
                path=Path(os.path.normpath(str(resolved))),
            )
        )

    raw_canvas = data.get("canvas")
    if raw_canvas is None:
        canvas = _infer_canvas(frames, warnings)
    else:
        if not isinstance(raw_canvas, dict):
            raise InputError("字段 canvas 必须是对象")
        canvas = (
            _require_int(raw_canvas.get("width"), "canvas.width", minimum=1),
            _require_int(raw_canvas.get("height"), "canvas.height", minimum=1),
        )

    raw_safe = data.get("safe_area")
    if raw_safe is None:
        safe_area = _default_safe_area(*canvas)
    else:
        safe_area = _parse_rect_dict(raw_safe, "safe_area")
        if (
            safe_area.width <= 0
            or safe_area.height <= 0
            or safe_area.left < 0
            or safe_area.top < 0
            or safe_area.right > canvas[0]
            or safe_area.bottom > canvas[1]
        ):
            raise InputError(
                f"安全区 {safe_area.as_dict()} 非法或超出画布 {canvas[0]}x{canvas[1]}"
            )

    panel, panel_status = _parse_panel(
        data.get("panel"), canvas[0], canvas[1], warnings
    )

    subtitles, blank_count = _parse_subtitles(data.get("subtitles"))
    if blank_count:
        warnings.append(f"已忽略 {blank_count} 条文本为空的字幕")

    style = _parse_style(data.get("style"))

    if not frames:
        warnings.append("清单中没有帧条目，预览帧序列为空")

    return Manifest(
        path=path,
        base_dir=base_dir,
        canvas=canvas,
        safe_area=safe_area,
        panel=panel,
        panel_status=panel_status,
        frames=tuple(frames),
        subtitles=tuple(subtitles),
        style=style,
        warnings=tuple(warnings),
    )
