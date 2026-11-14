"""Manifest loading and validation.

Validation failures raise :class:`ManifestError`, whose messages always name the
offending field (for example ``canvas.width`` or ``safe_margin.top``) so the
operator can fix the manifest without reading a traceback.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(Exception):
    """Raised when the manifest cannot be read or fails structural validation."""

    def __init__(self, messages: list[str], source: Path | None = None):
        self.messages = messages
        self.source = source
        detail = "\n".join(f"  - {m}" for m in messages)
        super().__init__(detail)


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int


@dataclass(frozen=True)
class Margin:
    top: int
    right: int
    bottom: int
    left: int


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class Subtitle:
    index: int
    id: str
    start_ms: int
    end_ms: int
    text: str
    font_size: float
    anchor_x: float
    anchor_y: float


@dataclass(frozen=True)
class Manifest:
    canvas: Canvas
    margin: Margin
    fx_region: Rect
    subtitles: tuple[Subtitle, ...]
    source: Path


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, field: str, message: str) -> None:
        self.items.append(f"{field}：{message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _required_int(obj: dict[str, Any], key: str, field: str, errs: _Errors, *, positive: bool) -> int | None:
    if key not in obj:
        errs.add(field, "缺少该字段")
        return None
    value = obj[key]
    if not _is_int(value):
        errs.add(field, f"必须为{('正' if positive else '非负')}整数（当前值 {_fmt(value)}）")
        return None
    if positive and value <= 0:
        errs.add(field, f"必须为正整数（当前值 {value}）")
        return None
    if not positive and value < 0:
        errs.add(field, f"必须为非负整数（当前值 {value}）")
        return None
    return value


def _required_number(obj: dict[str, Any], key: str, field: str, errs: _Errors, *, positive: bool) -> float | None:
    if key not in obj:
        errs.add(field, "缺少该字段")
        return None
    value = obj[key]
    if not _is_number(value):
        errs.add(field, f"必须为数字（当前值 {_fmt(value)}）")
        return None
    number = float(value)
    if not math.isfinite(number):
        errs.add(field, f"必须为有限数字（当前值 {_fmt(value)}）")
        return None
    if positive and number <= 0:
        errs.add(field, f"必须为正数（当前值 {value}）")
        return None
    return number


def _require_object(value: Any, field: str, errs: _Errors) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errs.add(field, f"必须为对象（当前值 {_fmt(value)}）")
        return None
    return value


def _parse_canvas(raw: Any, errs: _Errors) -> Canvas | None:
    obj = _require_object(raw, "canvas", errs)
    if obj is None:
        return None
    width = _required_int(obj, "width", "canvas.width", errs, positive=True)
    height = _required_int(obj, "height", "canvas.height", errs, positive=True)
    if width is None or height is None:
        return None
    return Canvas(width=width, height=height)


def _parse_margin(raw: Any, canvas: Canvas | None, errs: _Errors) -> Margin | None:
    obj = _require_object(raw, "safe_margin", errs)
    if obj is None:
        return None
    values: dict[str, int | None] = {}
    for key, field in (
        ("top", "safe_margin.top"),
        ("right", "safe_margin.right"),
        ("bottom", "safe_margin.bottom"),
        ("left", "safe_margin.left"),
    ):
        values[key] = _required_int(obj, key, field, errs, positive=True)

    # "positive and no more than half of the corresponding canvas edge"
    if canvas is not None:
        checks = (
            ("top", "safe_margin.top", canvas.height, "高度"),
            ("bottom", "safe_margin.bottom", canvas.height, "高度"),
            ("left", "safe_margin.left", canvas.width, "宽度"),
            ("right", "safe_margin.right", canvas.width, "宽度"),
        )
        for key, field, edge, edge_label in checks:
            value = values[key]
            if value is None:
                continue
            limit = edge / 2
            if value > limit:
                errs.add(
                    field,
                    f"不能超过画布{edge_label}的一半 {limit:g}（当前值 {value}）",
                )

    if any(v is None for v in values.values()):
        return None
    return Margin(top=values["top"], right=values["right"], bottom=values["bottom"], left=values["left"])  # type: ignore[arg-type]


def _parse_fx_region(raw: Any, errs: _Errors) -> Rect | None:
    obj = _require_object(raw, "fx_region", errs)
    if obj is None:
        return None
    x = _required_int(obj, "x", "fx_region.x", errs, positive=False)
    y = _required_int(obj, "y", "fx_region.y", errs, positive=False)
    width = _required_int(obj, "width", "fx_region.width", errs, positive=True)
    height = _required_int(obj, "height", "fx_region.height", errs, positive=True)
    if x is None or y is None or width is None or height is None:
        return None
    return Rect(x=x, y=y, width=width, height=height)


def _parse_subtitle(raw: Any, index: int, errs: _Errors) -> Subtitle | None:
    prefix = f"subtitles[{index}]"
    obj = _require_object(raw, prefix, errs)
    if obj is None:
        return None

    sid = obj.get("id")
    if "id" not in obj:
        errs.add(f"{prefix}.id", "缺少该字段")
        sid_ok = False
    elif not isinstance(sid, str):
        errs.add(f"{prefix}.id", f"必须为字符串（当前值 {_fmt(sid)}）")
        sid_ok = False
    elif not sid.strip():
        errs.add(f"{prefix}.id", "不能为空字符串")
        sid_ok = False
    else:
        sid_ok = True

    start = _required_int(obj, "start_ms", f"{prefix}.start_ms", errs, positive=False)
    end = _required_int(obj, "end_ms", f"{prefix}.end_ms", errs, positive=False)

    text = obj.get("text")
    if "text" not in obj:
        errs.add(f"{prefix}.text", "缺少该字段")
        text_ok = False
    elif not isinstance(text, str):
        errs.add(f"{prefix}.text", f"必须为字符串（当前值 {_fmt(text)}）")
        text_ok = False
    elif not text.strip():
        errs.add(f"{prefix}.text", "不能为空字符串")
        text_ok = False
    else:
        text_ok = True

    font_size = _required_number(obj, "font_size", f"{prefix}.font_size", errs, positive=True)

    anchor = obj.get("anchor")
    anchor_ok = False
    anchor_x = anchor_y = None
    if "anchor" not in obj:
        errs.add(f"{prefix}.anchor", "缺少该字段")
    else:
        anchor_obj = _require_object(anchor, f"{prefix}.anchor", errs)
        if anchor_obj is not None:
            anchor_x = _required_number(anchor_obj, "x", f"{prefix}.anchor.x", errs, positive=False)
            anchor_y = _required_number(anchor_obj, "y", f"{prefix}.anchor.y", errs, positive=False)
            anchor_ok = anchor_x is not None and anchor_y is not None

    if not all((sid_ok, start is not None, end is not None, text_ok, font_size is not None, anchor_ok)):
        return None
    return Subtitle(
        index=index,
        id=sid,
        start_ms=start,
        end_ms=end,
        text=text,
        font_size=font_size,
        anchor_x=anchor_x,
        anchor_y=anchor_y,
    )


def validate_manifest(raw: Any, source: Path) -> Manifest:
    """Validate a decoded JSON document and return the typed manifest."""
    errs = _Errors()
    if not isinstance(raw, dict):
        raise ManifestError([f"manifest：顶层必须为 JSON 对象（当前值 {_fmt(raw)}）"], source)

    canvas = _parse_canvas(raw.get("canvas"), errs)

    if "safe_margin" not in raw:
        errs.add("safe_margin", "缺少该字段")
        margin = None
    else:
        margin = _parse_margin(raw.get("safe_margin"), canvas, errs)

    if "fx_region" not in raw:
        errs.add("fx_region", "缺少该字段（特效区矩形）")
        fx_region = None
    else:
        fx_region = _parse_fx_region(raw.get("fx_region"), errs)

    subtitles_raw = raw.get("subtitles")
    subtitles: list[Subtitle] = []
    if "subtitles" not in raw:
        errs.add("subtitles", "缺少该字段")
    elif not isinstance(subtitles_raw, list):
        errs.add("subtitles", f"必须为数组（当前值 {_fmt(subtitles_raw)}）")
    else:
        seen: dict[str, int] = {}
        for index, item in enumerate(subtitles_raw):
            sub = _parse_subtitle(item, index, errs)
            if sub is None:
                continue
            if sub.id in seen:
                errs.add(
                    f"subtitles[{index}].id",
                    f"与 subtitles[{seen[sub.id]}].id 重复（{_fmt(sub.id)}），条目 id 必须唯一",
                )
                continue
            seen[sub.id] = index
            subtitles.append(sub)

    if errs.items:
        raise ManifestError(errs.items, source)
    assert canvas is not None and margin is not None and fx_region is not None
    return Manifest(
        canvas=canvas,
        margin=margin,
        fx_region=fx_region,
        subtitles=tuple(subtitles),
        source=source,
    )


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate a manifest JSON file."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ManifestError([f"manifest：文件不存在（{source}）"], source) from None
    except UnicodeDecodeError as exc:
        raise ManifestError([f"manifest：不是有效的 UTF-8 文件（{exc.reason}）"], source) from None
    except OSError as exc:
        raise ManifestError([f"manifest：无法读取文件（{exc.strerror or exc}）"], source) from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            [f"manifest：JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"],
            source,
        ) from None
    return validate_manifest(raw, source)
