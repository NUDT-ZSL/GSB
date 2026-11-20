"""门限判定的稳定性保护。

提供两种确定性策略：

* ``strict`` —— 严格比较，相等时按 ``band_on_tie`` 显式裁决，不依赖
  浮点运算的偶然舍入方向；
* ``band`` —— 容差带比较，门限附近 ``[threshold-tolerance,
  threshold+tolerance]`` 内的取值统一按 tie 规则裁决，保证恰好落在门限
  附近的任务在重复运行下不会被舍入翻转。

同时提供次正规数（subnormal）保护：绝对值低于地板的量被夹到零或地板，
避免次正规数在不同硬件/编译选项下的行为差异。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import GateConfig
from .errors import EConfigInvalid

STRICT = "strict"
BAND = "band"
TIE_ACCEPT = "accept"
TIE_REJECT = "reject"


def sanitize_subnormal(value: float, floor: float) -> float:
    """把次正规数/极小量夹到确定性地板，非有限值原样上抛由调用方处理。"""
    if not math.isfinite(value):
        return value
    if value == 0.0:
        return 0.0
    if abs(value) < floor:
        return math.copysign(floor, value)
    return value


def sanitize_array(array, floor: float):
    """数组版本的次正规数保护，返回新数组。"""
    import numpy as np

    arr = np.asarray(array, dtype=np.float64)
    tiny = np.isfinite(arr) & (np.abs(arr) < floor) & (arr != 0.0)
    if tiny.any():
        arr = arr.copy()
        arr[tiny] = np.copysign(floor, arr[tiny])
    return arr


def validate_gate_config(config: GateConfig) -> None:
    """校验门限配置，非法时抛 :class:`EConfigInvalid` 并指明字段与取值。"""
    if not isinstance(config.threshold, (int, float)) or not math.isfinite(config.threshold):
        raise EConfigInvalid(
            f"字段 {config.field_name}.threshold 必须为有限数值",
            field=f"{config.field_name}.threshold",
            value=repr(config.threshold),
        )
    if config.strategy not in (STRICT, BAND):
        raise EConfigInvalid(
            f"字段 {config.field_name}.strategy 取值非法",
            field=f"{config.field_name}.strategy",
            value=repr(config.strategy),
        )
    if not isinstance(config.tolerance, (int, float)) or config.tolerance < 0 or not math.isfinite(
        config.tolerance
    ):
        raise EConfigInvalid(
            f"字段 {config.field_name}.tolerance 必须为非负有限数值",
            field=f"{config.field_name}.tolerance",
            value=repr(config.tolerance),
        )
    if config.strategy == STRICT and config.tolerance != 0.0:
        raise EConfigInvalid(
            f"strict 策略下 {config.field_name}.tolerance 必须为 0",
            field=f"{config.field_name}.tolerance",
            value=repr(config.tolerance),
        )
    if config.band_on_tie not in (TIE_ACCEPT, TIE_REJECT):
        raise EConfigInvalid(
            f"字段 {config.field_name}.band_on_tie 取值非法",
            field=f"{config.field_name}.band_on_tie",
            value=repr(config.band_on_tie),
        )


def validate_transfer_strength(strength: float, gate: GateConfig) -> float:
    """迁移强度合法性校验（必须是 [0, 1] 内有限浮点）。"""
    if not isinstance(strength, (int, float)) or not math.isfinite(strength):
        raise EConfigInvalid(
            "迁移强度必须为有限数值",
            field="transfer_strength",
            value=repr(strength),
        )
    strength = float(strength)
    if strength < 0.0 or strength > 1.0:
        raise EConfigInvalid(
            "迁移强度必须落在 [0, 1] 区间",
            field="transfer_strength",
            value=repr(strength),
        )
    validate_gate_config(gate)
    return strength


@dataclass(frozen=True)
class GateDecision:
    """门限裁决结果，附带判定依据便于审计。"""

    accepted: bool
    in_tie_band: bool
    distance: float
    reason: str


def evaluate_gate(value: float, config: GateConfig, subnormal_floor: float = 1e-300) -> GateDecision:
    """按配置对单个数值做门限裁决。

    判定方向为 ``value >= threshold`` 准入；tie（严格相等或落在容差带内）
    按 :attr:`GateConfig.band_on_tie` 显式裁决，从而消除舍入翻转。
    """
    validate_gate_config(config)
    safe_value = sanitize_subnormal(float(value), subnormal_floor)
    safe_threshold = sanitize_subnormal(float(config.threshold), subnormal_floor)
    if not math.isfinite(safe_value):
        raise EConfigInvalid(
            f"字段 {config.field_name} 参与判定时不是有限值",
            field=config.field_name,
            value=repr(value),
        )
    distance = safe_value - safe_threshold
    if config.strategy == STRICT:
        if distance == 0.0:
            accepted = config.band_on_tie == TIE_ACCEPT
            return GateDecision(accepted, True, distance, f"strict: 等于门限，tie={config.band_on_tie}")
        accepted = distance > 0.0
        return GateDecision(accepted, False, distance, "strict: 非门限点严格比较")
    if abs(distance) <= config.tolerance:
        accepted = config.band_on_tie == TIE_ACCEPT
        return GateDecision(
            accepted,
            True,
            distance,
            f"band: 落在容差带 ±{config.tolerance}，tie={config.band_on_tie}",
        )
    accepted = distance > 0.0
    return GateDecision(accepted, False, distance, "band: 容差带外按方向比较")
