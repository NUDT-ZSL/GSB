"""配置加载与严格校验。

配置字段（均可缺省，缺省值见 DEFAULT_CONFIG）：
    gain_db      增益，范围 [-20, +20] dB，默认 0
    threshold_db 静音阈值，范围 [-60, -20] dB，默认 -50
    fade_ms      首尾线性淡入淡出时长，0 表示不做淡变，否则范围 [10, 500] ms，默认 0
    order        处理顺序，由 "trim" / "fade" / "gain" 组成的数组，
                 默认 ["trim", "fade", "gain"]（裁剪 → 淡入淡出 → 增益）

任何无法解析、字段非法（含未知字段、类型错误、取值越界）都会抛出
ConfigError，由调用方保证“不处理任何素材”。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

# 合法取值区间
GAIN_RANGE = (-20.0, 20.0)
THRESHOLD_RANGE = (-60.0, -20.0)
FADE_RANGE_MS = (10.0, 500.0)
SAMPLE_RATE_RANGE = (8000, 48000)  # 与 wav_io 校验保持一致，仅用于文档引用

STEP_TRIM = "trim"
STEP_FADE = "fade"
STEP_GAIN = "gain"
ALL_STEPS = (STEP_TRIM, STEP_FADE, STEP_GAIN)
DEFAULT_ORDER = (STEP_TRIM, STEP_FADE, STEP_GAIN)


class ConfigError(ValueError):
    """配置文件无法解析或包含非法内容。"""


@dataclass(frozen=True)
class PipelineConfig:
    """一次批处理使用的完整配置（冻结，防止处理中途被篡改）。"""

    gain_db: float = 0.0
    threshold_db: float = -50.0
    fade_ms: float = 0.0
    order: tuple[str, ...] = field(default_factory=lambda: tuple(DEFAULT_ORDER))

    @property
    def order_list(self) -> list[str]:
        return list(self.order)


DEFAULT_CONFIG = PipelineConfig()

# JSON 键 -> 允许的 Python 数值类型（bool 是 int 的子类，需显式排除）
_KNOWN_KEYS = {"gain_db", "threshold_db", "fade_ms", "order"}


def _is_real_number(value: Any) -> bool:
    """仅接受 int/float 且非布尔、非 NaN、非无穷。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _check_range(name: str, value: Any, low: float, high: float, unit: str = "") -> float:
    if not _is_real_number(value):
        raise ConfigError(f"配置项 {name!r} 必须是一个数字，实际得到：{value!r}")
    num = float(value)
    if not (low <= num <= high):
        raise ConfigError(
            f"配置项 {name!r} 取值 {num}{unit} 超出允许范围 [{low}, {high}]{unit}"
        )
    return num


def _validate_order(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"配置项 'order' 必须是数组，实际得到：{value!r}")
    if len(value) != 3:
        raise ConfigError(f"配置项 'order' 必须恰好包含 3 个步骤，实际为 {len(value)} 个")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"配置项 'order' 中的元素必须是字符串，实际得到：{item!r}")
        if item not in ALL_STEPS:
            raise ConfigError(
                f"配置项 'order' 含未知步骤 {item!r}，合法值为 {list(ALL_STEPS)}"
            )
        if item in result:
            raise ConfigError(f"配置项 'order' 中步骤 {item!r} 重复出现，每个步骤只能出现一次")
        result.append(item)
    return tuple(result)


def parse_config(data: Any) -> PipelineConfig:
    """把已解析的 JSON 对象校验为 PipelineConfig。"""
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是 JSON 对象，例如 {\"gain_db\": 6}")

    unknown = set(data.keys()) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"配置含未知字段 {sorted(unknown)}，合法字段为 {sorted(_KNOWN_KEYS)}"
        )

    gain_db = (
        _check_range("gain_db", data["gain_db"], *GAIN_RANGE, unit="dB")
        if "gain_db" in data
        else DEFAULT_CONFIG.gain_db
    )
    threshold_db = (
        _check_range("threshold_db", data["threshold_db"], *THRESHOLD_RANGE, unit="dB")
        if "threshold_db" in data
        else DEFAULT_CONFIG.threshold_db
    )

    fade_ms = DEFAULT_CONFIG.fade_ms
    if "fade_ms" in data:
        raw_fade = data["fade_ms"]
        if raw_fade != 0:  # 0 是唯一允许落在 [10,500] 之外的值，表示关闭淡变
            fade_ms = _check_range("fade_ms", raw_fade, *FADE_RANGE_MS, unit="ms")

    order = _validate_order(data["order"]) if "order" in data else tuple(DEFAULT_ORDER)

    return PipelineConfig(
        gain_db=gain_db,
        threshold_db=threshold_db,
        fade_ms=fade_ms,
        order=order,
    )


def load_config(path: str | Path) -> PipelineConfig:
    """读取并校验配置文件；任何问题统一抛 ConfigError（含中文说明）。"""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在：{config_path}") from None
    except OSError as exc:
        raise ConfigError(f"配置文件无法读取：{config_path}（{exc}）") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from None

    return parse_config(data)


def dump_default_config() -> dict[str, Any]:
    """返回默认配置的可序列化形式，便于生成样例配置。"""
    return {
        "gain_db": DEFAULT_CONFIG.gain_db,
        "threshold_db": DEFAULT_CONFIG.threshold_db,
        "fade_ms": DEFAULT_CONFIG.fade_ms,
        "order": list(DEFAULT_ORDER),
    }
