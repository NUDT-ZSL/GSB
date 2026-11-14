"""联系表配置的加载与校验。

约定的固定默认色（设计对照基线）：
    背景色 #F2F2F2（浅灰），网格线色 #333333（深灰）。
两者均可被配置 JSON 的 background_color / grid_color 覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import ConfigError

# 约定的固定默认颜色
DEFAULT_BACKGROUND_COLOR = "#F2F2F2"
DEFAULT_GRID_COLOR = "#333333"
DEFAULT_TEXT_COLOR = "#111111"
DEFAULT_HEADER_COLOR = "#111111"
DEFAULT_LABEL_TEMPLATE = "帧 {frame} | {ms} ms"

# 尺寸硬边界，越界即 ConfigError，保证不会产出畸形或超大画布
_LIMITS = {
    "thumb_width": (16, 2000),
    "thumb_height": (16, 2000),
    "columns": (1, 20),
    "spacing": (0, 200),
    "padding": (0, 200),
    "font_size": (8, 96),
}


def _parse_hex_color(value, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"配置项 {field} 必须是 #RRGGBB 十六进制颜色字符串，实际为 {type(value).__name__}")
    text = value.strip()
    if len(text) != 7 or text[0] != "#":
        raise ConfigError(f"配置项 {field} 颜色格式非法：{value!r}，应为 #RRGGBB")
    try:
        int(text[1:], 16)
    except ValueError as exc:
        raise ConfigError(f"配置项 {field} 颜色格式非法：{value!r}，应为 #RRGGBB") from exc
    return text.upper()


def _check_int(raw, field: str) -> int:
    # bool 是 int 的子类，显式排除
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"配置项 {field} 必须是整数，实际为 {type(raw).__name__}")
    low, high = _LIMITS[field]
    if not low <= raw <= high:
        raise ConfigError(f"配置项 {field}={raw} 越界，允许范围为 [{low}, {high}]")
    return raw


@dataclass(frozen=True)
class SheetConfig:
    thumb_width: int
    thumb_height: int
    columns: int
    spacing: int
    padding: int
    font_size: int
    label_template: str
    background_color: str
    grid_color: str
    text_color: str
    header_color: str

    def to_dict(self) -> dict:
        return {
            "thumb_width": self.thumb_width,
            "thumb_height": self.thumb_height,
            "columns": self.columns,
            "spacing": self.spacing,
            "padding": self.padding,
            "font_size": self.font_size,
            "label_template": self.label_template,
            "background_color": self.background_color,
            "grid_color": self.grid_color,
            "text_color": self.text_color,
            "header_color": self.header_color,
        }


def load_config(path: str | None) -> SheetConfig:
    """加载配置 JSON；path 为 None 或文件中缺省的字段全部回落到约定默认值。"""
    raw: dict = {}
    if path is not None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在：{path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{path}（第 {exc.lineno} 行）") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("配置 JSON 顶层必须是对象")
        raw = loaded

    values = {
        "thumb_width": raw.get("thumb_width", 320),
        "thumb_height": raw.get("thumb_height", 180),
        "columns": raw.get("columns", 4),
        "spacing": raw.get("spacing", 12),
        "padding": raw.get("padding", 24),
        "font_size": raw.get("font_size", 14),
    }
    for field, value in values.items():
        values[field] = _check_int(value, field)

    label_template = raw.get("label_template", DEFAULT_LABEL_TEMPLATE)
    if not isinstance(label_template, str) or not label_template.strip():
        raise ConfigError("配置项 label_template 必须是非空字符串")
    if "{frame}" not in label_template:
        raise ConfigError("配置项 label_template 必须包含占位符 {frame}")

    return SheetConfig(
        label_template=label_template,
        background_color=_parse_hex_color(raw.get("background_color", DEFAULT_BACKGROUND_COLOR),
                                          "background_color"),
        grid_color=_parse_hex_color(raw.get("grid_color", DEFAULT_GRID_COLOR), "grid_color"),
        text_color=_parse_hex_color(raw.get("text_color", DEFAULT_TEXT_COLOR), "text_color"),
        # 表头色缺省跟随文字色，保证深色背景下仍可读
        header_color=_parse_hex_color(raw.get("header_color", raw.get("text_color", DEFAULT_HEADER_COLOR)),
                                      "header_color"),
        **values,
    )
