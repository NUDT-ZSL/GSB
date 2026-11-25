"""可切换精度档：float64 与定点缩放整数共用同一算子语义。

两条路径实现完全相同的算子集合（加、减、乘、除、开方、比较、量化），
召回与融合代码只依赖 :class:`Ops` 抽象，不感知底层是浮点还是整数。
定点档所有数值都是乘以 ``scale`` 后的整数，乘除与开方都按配置的
舍入模式显式舍入，因此误差来源可追溯、可复现。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .errors import BusinessError, ErrorCode

MODE_FLOAT64 = "float64"
MODE_FIXED = "fixed"

ROUNDING_MODES = ("half_up", "half_down", "half_even", "truncate")


@dataclass(frozen=True)
class PrecisionConfig:
    """精度档配置。

    ``scale`` 为定点缩放因子（例如 1_000_000 表示 10^-6 的量化步长），
    ``rounding`` 为定点算子的显式舍入模式。
    float64 档下这两个字段仅用于诊断记录，不参与运算。
    """

    mode: str = MODE_FLOAT64
    scale: int = 1_000_000
    rounding: str = "half_even"

    def validate(self) -> None:
        if self.mode not in (MODE_FLOAT64, MODE_FIXED):
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                f"未知精度档: {self.mode}",
                field_path="precision.mode",
            )
        if not isinstance(self.scale, int) or self.scale <= 0:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "定点缩放因子必须为正整数",
                field_path="precision.scale",
            )
        if self.rounding not in ROUNDING_MODES:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                f"未知舍入模式: {self.rounding}，可选 {ROUNDING_MODES}",
                field_path="precision.rounding",
            )


def _quantize_ratio(numerator: int, denominator: int, rounding: str) -> int:
    """按指定舍入模式计算 numerator / denominator 的整数结果（支持负数）。"""
    if denominator == 0:
        raise ZeroDivisionError("定点除法令分母为零")
    sign = -1 if (numerator < 0) ^ (denominator < 0) else 1
    n = abs(numerator)
    d = abs(denominator)
    q, r = divmod(n, d)
    if r != 0:
        twice = 2 * r
        if rounding == "truncate":
            up = False
        elif rounding == "half_up":
            up = twice >= d
        elif rounding == "half_down":
            up = twice > d
        else:  # half_even：银行家舍入
            up = twice > d or (twice == d and q % 2 == 1)
        if up:
            q += 1
    return sign * q


class Ops(Protocol):
    """两条精度路径共同实现的算子协议。"""

    mode: str

    def from_float(self, value: float) -> object: ...

    def to_float(self, value: object) -> float: ...

    def add(self, a, b) -> object: ...

    def sub(self, a, b) -> object: ...

    def mul(self, a, b) -> object: ...

    def div(self, a, b) -> object: ...

    def sqrt(self, a) -> object: ...

    def maximum(self, a, b) -> object: ...

    def minimum(self, a, b) -> object: ...


class Float64Ops:
    """float64 参考路径，直接使用 IEEE-754 双精度运算。"""

    mode = MODE_FLOAT64

    def __init__(self, config: PrecisionConfig) -> None:
        self.config = config

    def from_float(self, value: float) -> float:
        return float(value)

    def to_float(self, value) -> float:
        return float(value)

    def add(self, a: float, b: float) -> float:
        return a + b

    def sub(self, a: float, b: float) -> float:
        return a - b

    def mul(self, a: float, b: float) -> float:
        return a * b

    def div(self, a: float, b: float) -> float:
        return a / b

    def sqrt(self, a: float) -> float:
        return math.sqrt(a)

    def maximum(self, a: float, b: float) -> float:
        return max(a, b)

    def minimum(self, a: float, b: float) -> float:
        return min(a, b)


class FixedOps:
    """定点缩放整数路径。

    领域内的整数 ``v`` 表示真实值 ``v / scale``。
    乘法与除法都会重新量化到同一缩放因子，开方使用整数牛顿取整，
    保证全部中间结果仍是定点整数，误差只来自显式量化。
    """

    mode = MODE_FIXED

    def __init__(self, config: PrecisionConfig) -> None:
        if config.mode != MODE_FIXED:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "FixedOps 要求 precision.mode=fixed",
                field_path="precision.mode",
            )
        self.config = config
        self.scale = config.scale
        self.rounding = config.rounding

    def from_float(self, value: float) -> int:
        scaled = value * self.scale
        sign = -1 if scaled < 0 else 1
        magnitude = abs(scaled)
        floor_value = math.floor(magnitude)
        fraction = magnitude - floor_value
        if fraction == 0:
            return sign * floor_value
        twice = 2 * fraction
        if self.rounding == "truncate":
            up = False
        elif self.rounding == "half_up":
            up = twice >= 1
        elif self.rounding == "half_down":
            up = twice > 1
        else:  # half_even
            up = twice > 1 or (twice == 1 and floor_value % 2 == 1)
        return sign * (floor_value + 1 if up else floor_value)

    def to_float(self, value: int) -> float:
        return value / self.scale

    def add(self, a: int, b: int) -> int:
        return a + b

    def sub(self, a: int, b: int) -> int:
        return a - b

    def mul(self, a: int, b: int) -> int:
        # (a/S)*(b/S) = a*b/S^2，领域结果 = a*b/S
        return _quantize_ratio(a * b, self.scale, self.rounding)

    def div(self, a: int, b: int) -> int:
        # (a/S)/(b/S) = a/b，领域结果 = a*S/b
        return _quantize_ratio(a * self.scale, b, self.rounding)

    def sqrt(self, a: int) -> int:
        if a < 0:
            raise ValueError("定点开方不支持负数")
        # sqrt(a/S) 的领域结果为 sqrt(a*S)，对整数结果做舍入
        target = a * self.scale
        base = math.isqrt(target)
        remainder = target - base * base
        if remainder == 0:
            return base
        gap = (base + 1) * (base + 1) - target
        twice_r = 2 * remainder
        d = remainder + gap  # 相邻整数间距在“余数口径”上的分母
        if self.rounding == "truncate":
            up = False
        elif self.rounding == "half_up":
            up = twice_r >= d
        elif self.rounding == "half_down":
            up = twice_r > d
        else:
            up = twice_r > d or (twice_r == d and base % 2 == 1)
        return base + 1 if up else base

    def maximum(self, a: int, b: int) -> int:
        return max(a, b)

    def minimum(self, a: int, b: int) -> int:
        return min(a, b)
def build_ops(config: PrecisionConfig) -> Ops:
    """按配置实例化对应精度路径的算子。"""
    config.validate()
    if config.mode == MODE_FLOAT64:
        return Float64Ops(config)
    return FixedOps(config)
