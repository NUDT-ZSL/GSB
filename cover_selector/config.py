"""读取并校验运行配置。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

_REQUIRED = ("fps", "base_width", "base_height", "dedupe_threshold")
_OPTIONAL = ("tolerance_frames", "tolerance_seconds", "frame_start", "resize_mode")
_ALLOWED = set(_REQUIRED) | set(_OPTIONAL)


@dataclass(frozen=True)
class Config:
    """工具的有效配置。

    ``tolerance_frames`` 以“帧”为单位；目标帧号 = frame_start + time * fps。
    配置也可写 ``tolerance_seconds``（以秒为单位），加载时会按 fps 换算为帧，
    两者只能出现其一。
    """

    fps: float
    base_width: int
    base_height: int
    dedupe_threshold: float
    tolerance_frames: float = 0.5
    frame_start: int = 0
    resize_mode: str = "fit"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置项 {name} 必须是数值，实际为 {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"配置项 {name} 必须是有限数值，实际为 {value!r}")
    return number


def _integer(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置项 {name} 必须是整数，实际为 {value!r}")
    if value < minimum:
        raise ConfigError(f"配置项 {name} 必须大于等于 {minimum}，实际为 {value}")
    return value


def load_config(path: Path) -> Config:
    """读取 JSON 配置；出现未知字段或非法值时抛出 :class:`ConfigError`。"""

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}（第 {exc.lineno} 行）") from exc

    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")

    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise ConfigError(f"配置包含未知字段：{', '.join(unknown)}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ConfigError(f"配置缺少必填字段：{', '.join(missing)}")

    fps = _finite_number(raw["fps"], "fps")
    if fps <= 0:
        raise ConfigError(f"配置项 fps 必须大于 0，实际为 {fps:g}")

    tolerance = _finite_number(raw.get("tolerance_frames", 0.5), "tolerance_frames")
    if "tolerance_seconds" in raw:
        if "tolerance_frames" in raw:
            raise ConfigError("tolerance_frames 与 tolerance_seconds 只能配置其中之一")
        seconds = _finite_number(raw["tolerance_seconds"], "tolerance_seconds")
        if seconds < 0:
            raise ConfigError(f"配置项 tolerance_seconds 不能为负数，实际为 {seconds:g}")
        tolerance = seconds * fps
    if tolerance < 0:
        raise ConfigError(f"帧号容差不能为负数，实际为 {tolerance:g} 帧")

    threshold = _finite_number(raw["dedupe_threshold"], "dedupe_threshold")
    if not 0 <= threshold <= 1:
        raise ConfigError(
            f"配置项 dedupe_threshold 必须在 0 到 1 之间，实际为 {threshold:g}"
        )

    resize_mode = raw.get("resize_mode", "fit")
    if resize_mode not in ("fit", "stretch"):
        raise ConfigError(
            f"配置项 resize_mode 只能是 'fit' 或 'stretch'，实际为 {resize_mode!r}"
        )

    return Config(
        fps=fps,
        base_width=_integer(raw["base_width"], "base_width", 1),
        base_height=_integer(raw["base_height"], "base_height", 1),
        dedupe_threshold=threshold,
        tolerance_frames=tolerance,
        frame_start=_integer(raw.get("frame_start", 0), "frame_start", 0),
        resize_mode=resize_mode,
    )
