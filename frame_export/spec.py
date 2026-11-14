"""Read and validate the animation description JSON.

The canonical schema is documented in README.md.  A few common aliases are
accepted so hand-written descriptions are not rejected for spelling alone,
but the canonical names are what the sample and the report use.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ExportError

DEFAULT_BACKGROUND = "#000000"


@dataclass(frozen=True)
class CanvasConfig:
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}


@dataclass(frozen=True)
class Background:
    value: str
    rgba: tuple[int, int, int, int]

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "rgba": list(self.rgba)}


@dataclass
class FrameSpec:
    ordinal: int
    index: Any
    file_ref: Any
    duration_ms: Any
    source_path: Path | None
    issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def duration_valid(self) -> bool:
        return _is_number(self.duration_ms) and self.duration_ms >= 0


@dataclass
class Spec:
    path: Path
    display_path: str
    canvas: CanvasConfig
    background: Background
    fps: float
    fps_raw: Any
    frames: list[FrameSpec]
    declared_frame_count: int | None
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ExportError(
            "spec_missing",
            f"找不到描述 JSON 文件：{path}",
            {"path": str(path)},
        ) from None
    except OSError as exc:
        raise ExportError(
            "spec_unreadable",
            f"无法读取描述 JSON 文件：{exc}",
            {"path": str(path)},
        ) from None

    def reject_constant(token: str) -> Any:
        raise ValueError(f"JSON 中不允许非有限数值 {token}")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExportError(
            "spec_malformed",
            f"描述 JSON 不是合法 JSON：{exc}",
            {"path": str(path)},
        ) from None


def _pick(mapping: dict[str, Any], names: tuple[str, ...]) -> tuple[bool, str, Any]:
    for name in names:
        if name in mapping:
            return True, name, mapping[name]
    return False, "", None


def _parse_canvas(root: dict[str, Any]) -> CanvasConfig:
    found, key, value = _pick(root, ("canvas", "size", "frame_size", "resolution"))
    width = height = None
    if found:
        if isinstance(value, dict):
            ok_w, _, width = _pick(value, ("width", "w"))
            ok_h, _, height = _pick(value, ("height", "h"))
            if not (ok_w and ok_h):
                raise ExportError(
                    "invalid_canvas",
                    "画幅 canvas 必须包含 width 与 height。",
                    {"canvas": value},
                )
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            width, height = value
        elif isinstance(value, str):
            parts = value.lower().replace(" ", "").split("x")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                width, height = int(parts[0]), int(parts[1])
        else:
            raise ExportError(
                "invalid_canvas",
                "画幅 canvas 必须是 {width,height} 对象或 [宽,高] 数组。",
                {"canvas": value},
            )
    else:
        ok_w, _, width = _pick(root, ("width", "canvas_width"))
        ok_h, _, height = _pick(root, ("height", "canvas_height"))
        if not (ok_w and ok_h):
            raise ExportError(
                "missing_canvas",
                "描述 JSON 缺少全局画幅 canvas（width/height）。",
                {},
            )

    if not (_is_int(width) and _is_int(height)):
        raise ExportError(
            "invalid_canvas",
            f"画幅宽高必须是整数，当前为 width={width!r}, height={height!r}。",
            {"width": width, "height": height},
        )
    if width <= 0 or height <= 0:
        raise ExportError(
            "invalid_canvas",
            f"画幅宽高必须大于 0，当前为 {width}x{height}。",
            {"width": width, "height": height},
        )
    return CanvasConfig(int(width), int(height))


_NAMED_COLORS = {
    "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "transparent": (0, 0, 0, 0),
    "red": (255, 0, 0, 255),
    "green": (0, 128, 0, 255),
    "blue": (0, 0, 255, 255),
    "gray": (128, 128, 128, 255),
    "grey": (128, 128, 128, 255),
}


def parse_background(value: Any) -> Background:
    if isinstance(value, str):
        text = value.strip()
        lower = text.lower()
        if lower in _NAMED_COLORS:
            return Background(text, _NAMED_COLORS[lower])
        if text.startswith("#"):
            hex_text = text[1:]
            if len(hex_text) in (3, 4):
                hex_text = "".join(ch * 2 for ch in hex_text)
            if len(hex_text) in (6, 8) and all(
                ch in "0123456789abcdefABCDEF" for ch in hex_text
            ):
                alpha = int(hex_text[6:8], 16) if len(hex_text) == 8 else 255
                rgba = (
                    int(hex_text[0:2], 16),
                    int(hex_text[2:4], 16),
                    int(hex_text[4:6], 16),
                    alpha,
                )
                return Background(text, rgba)
        raise ExportError(
            "invalid_background",
            f"背景色无法解析：{value!r}。请使用 #RGB、#RRGGBB、#RRGGBBAA 或 [r,g,b] 数组。",
            {"background": value},
        )
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        parts = list(value)
        if all(_is_int(part) and 0 <= part <= 255 for part in parts):
            rgba = tuple(parts + ([255] if len(parts) == 3 else []))
            assert len(rgba) == 4
            return Background(
                "#{:02X}{:02X}{:02X}{:02X}".format(*rgba), rgba  # type: ignore[arg-type]
            )
        raise ExportError(
            "invalid_background",
            "背景色数组的 3 或 4 个分量必须是 0..255 的整数。",
            {"background": value},
        )
    raise ExportError(
        "invalid_background",
        f"背景色类型不支持：{type(value).__name__}。",
        {"background": value},
    )


def _parse_fps(root: dict[str, Any]) -> tuple[float, Any]:
    found, _, value = _pick(root, ("fps", "frame_rate", "frameRate", "framerate"))
    if not found:
        raise ExportError(
            "missing_fps",
            "描述 JSON 缺少全局帧率 fps。",
            {},
        )
    if not _is_number(value) or float(value) <= 0:
        shown = "非数值" if not _is_number(value) else repr(value)
        raise ExportError(
            "invalid_fps",
            f"帧率(fps)必须为大于 0 的数值，当前为 {shown}。",
            {"fps": value},
        )
    return float(value), value


def _parse_declared_count(root: dict[str, Any]) -> int | None:
    found, _, value = _pick(
        root, ("frame_count", "frameCount", "total_frames", "totalFrames", "frames_total")
    )
    if not found:
        return None
    if not (_is_int(value) and value >= 0):
        raise ExportError(
            "invalid_frame_count",
            f"frame_count 必须是非负整数，当前为 {value!r}。",
            {"frame_count": value},
        )
    return int(value)


def _check_file_ref(ref: Any, spec_dir: Path) -> tuple[Path | None, str | None]:
    """Resolve a frame reference, confining it to the description directory."""
    if not isinstance(ref, str) or not ref.strip():
        return None, "帧文件引用必须是非空字符串"
    candidate_text = ref.strip().replace("\\", "/")
    if os.path.isabs(candidate_text) or Path(candidate_text).is_absolute():
        return None, "帧文件引用必须是描述 JSON 所在目录内的相对路径"
    base = spec_dir.resolve()
    candidate = (base / candidate_text).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None, "帧文件引用不能越出描述 JSON 所在目录"
    return candidate, None


def _parse_frame(
    ordinal: int,
    raw: Any,
    spec_dir: Path,
) -> FrameSpec:
    issues: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return FrameSpec(
            ordinal=ordinal,
            index=None,
            file_ref=None,
            duration_ms=None,
            source_path=None,
            issues=[
                {
                    "code": "invalid_frame_entry",
                    "message": "帧条目必须是 JSON 对象。",
                }
            ],
        )

    ok_index, _, index = _pick(
        raw, ("index", "seq", "sequence", "frame_index", "number", "no")
    )
    if not ok_index:
        issues.append(
            {"code": "missing_index", "message": "帧条目缺少序号 index。"}
        )
        index = None

    ok_file, _, file_ref = _pick(
        raw, ("file", "path", "filename", "src", "image", "frame_file")
    )
    if not ok_file:
        issues.append(
            {"code": "missing_file", "message": "帧条目缺少文件引用 file。"}
        )
        file_ref = None

    ok_duration, _, duration_raw = _pick(
        raw,
        (
            "duration_ms",
            "durationMs",
            "duration",
            "hold_ms",
            "holdMs",
            "dwell_ms",
            "stay_ms",
            "delay_ms",
        ),
    )
    if not ok_duration:
        issues.append(
            {
                "code": "missing_duration",
                "message": "帧条目缺少停留毫秒 duration_ms。",
            }
        )
        duration_raw = None

    source_path, path_issue = _check_file_ref(file_ref, spec_dir)
    if path_issue:
        issues.append({"code": "invalid_file_path", "message": path_issue})

    duration_ms = duration_raw
    if ok_duration and not _is_number(duration_raw):
        issues.append(
            {
                "code": "invalid_duration",
                "message": f"停留毫秒 duration_ms 不是数值：{duration_raw!r}。",
            }
        )
    elif ok_duration and float(duration_raw) < 0:
        issues.append(
            {
                "code": "invalid_duration",
                "message": f"停留毫秒 duration_ms 不能为负数：{duration_raw!r}。",
            }
        )

    if index is not None and not _is_int(index):
        issues.append(
            {
                "code": "invalid_index",
                "message": f"帧序号 index 必须是整数：{index!r}。",
            }
        )
    elif _is_int(index) and index < 0:
        issues.append(
            {
                "code": "invalid_index",
                "message": f"帧序号 index 不能为负数：{index!r}。",
            }
        )

    return FrameSpec(
        ordinal=ordinal,
        index=index,
        file_ref=file_ref,
        duration_ms=duration_ms,
        source_path=source_path,
        issues=issues,
    )


def parse_spec(path: str | Path) -> Spec:
    spec_path = Path(path)
    raw = _read_json(spec_path)
    if not isinstance(raw, dict):
        raise ExportError(
            "spec_invalid",
            "描述 JSON 的顶层必须是对象。",
            {},
        )

    canvas = _parse_canvas(raw)
    has_background, _, background_value = _pick(
        raw,
        (
            "background",
            "background_color",
            "backgroundColor",
            "bg",
            "bg_color",
            "bgColor",
        ),
    )
    if not has_background:
        background_value = DEFAULT_BACKGROUND
    background = parse_background(background_value)
    fps, fps_raw = _parse_fps(raw)
    declared_count = _parse_declared_count(raw)

    found, key, raw_frames = _pick(raw, ("frames", "frame_list", "sequence"))
    if not found:
        raise ExportError(
            "missing_frames",
            "描述 JSON 缺少帧数组 frames。",
            {},
        )
    if not isinstance(raw_frames, list):
        raise ExportError(
            "invalid_frames",
            "帧数组 frames 必须是 JSON 数组。",
            {},
        )

    spec_dir = spec_path.resolve().parent
    frames = [
        _parse_frame(ordinal, entry, spec_dir)
        for ordinal, entry in enumerate(raw_frames, start=1)
    ]

    warnings: list[dict[str, Any]] = []
    if not has_background:
        warnings.append(
            {
                "code": "default_background",
                "message": f"描述未提供背景色，使用默认值 {DEFAULT_BACKGROUND}。",
            }
        )

    return Spec(
        path=spec_path,
        display_path=_display_path(spec_path),
        canvas=canvas,
        background=background,
        fps=fps,
        fps_raw=fps_raw,
        frames=frames,
        declared_frame_count=declared_count,
        warnings=warnings,
    )


def _display_path(path: Path) -> str:
    """Stable forward-slash path for report/preview output."""
    try:
        text = os.path.relpath(os.path.abspath(path), os.getcwd())
    except ValueError:
        text = str(path)
    return Path(text).as_posix()
