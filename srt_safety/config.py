"""JSON 画幅配置加载与校验。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置缺失字段或取值非法。"""


@dataclass(frozen=True)
class CanvasConfig:
    width: int
    height: int
    fps: float
    safe_ratio_x: float
    safe_ratio_y: float
    font_size: int
    line_height: int

    @property
    def safe_x(self) -> int:
        """安全区左边距（像素）。"""
        return round(self.width * self.safe_ratio_x)

    @property
    def safe_y(self) -> int:
        """安全区上边距（像素）。"""
        return round(self.height * self.safe_ratio_y)

    @property
    def safe_w(self) -> int:
        return self.width - 2 * self.safe_x

    @property
    def safe_h(self) -> int:
        return self.height - 2 * self.safe_y


def _number(data: dict, key: str) -> float:
    if key not in data:
        raise ConfigError(f"配置项 '{key}' 缺失，请在 JSON 中提供该字段")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置项 '{key}' 必须是数值，当前值为 {value!r}")
    return float(value)


def _positive_int(data: dict, key: str) -> int:
    value = _number(data, key)
    if value <= 0 or not float(value).is_integer():
        raise ConfigError(f"配置项 '{key}' 必须为正整数（像素），当前值为 {value:g}")
    return int(value)


def _ratio(data: dict, key: str) -> float:
    value = _number(data, key)
    if not 0 < value < 0.5:
        raise ConfigError(
            f"配置项 '{key}' 为安全区留白比例，必须位于开区间 (0, 0.5)，当前值为 {value:g}"
        )
    return value


def load_config(path: str | Path) -> CanvasConfig:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {config_path}")
    except OSError as exc:
        raise ConfigError(f"配置文件无法读取: {config_path}（{exc}）")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法 JSON: {config_path}，第 {exc.lineno} 行: {exc.msg}"
        )
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")

    width = _positive_int(data, "width")
    height = _positive_int(data, "height")

    fps = _number(data, "fps")
    if fps <= 0:
        raise ConfigError(f"配置项 'fps' 帧率必须大于 0，当前值为 {fps:g}")

    ratio = _ratio(data, "safe_ratio")
    ratio_x = _ratio(data, "safe_ratio_x") if "safe_ratio_x" in data else ratio
    ratio_y = _ratio(data, "safe_ratio_y") if "safe_ratio_y" in data else ratio

    font_size = _positive_int(data, "font_size")
    line_height = _positive_int(data, "line_height")
    if line_height < font_size:
        raise ConfigError(
            f"配置项 'line_height'({line_height}) 不能小于 'font_size'({font_size})，"
            "否则行会互相压盖"
        )

    cfg = CanvasConfig(width, height, fps, ratio_x, ratio_y, font_size, line_height)
    if cfg.safe_w <= 0:
        raise ConfigError(
            f"安全区水平宽度非法: 画幅宽 {width} 扣除两侧留白后仅剩 {cfg.safe_w}px，"
            "请检查 'width' 与 'safe_ratio' / 'safe_ratio_x'"
        )
    if cfg.safe_h < line_height:
        raise ConfigError(
            f"安全区垂直高度 {cfg.safe_h}px 小于单行行高 {line_height}px，"
            "请检查 'height'、'line_height' 与 'safe_ratio' / 'safe_ratio_y'"
        )
    return cfg
