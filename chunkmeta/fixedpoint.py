"""受控定点换算核心。

分片大小、缓存占用与读取延迟都按声明标度换算为定点整数。
每次换算登记：原始浮点值、精确放大值、舍入余量、舍入方向。
未满一个定点单位的余量逐项保留，并在累加器中参与后续累加。
换算基于 Fraction（对二进制浮点精确），同一输入重复换算逐位一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import List


@dataclass(frozen=True)
class ConversionRecord:
    stage: str               # 换算所属环节名
    original: float          # 原始浮点值（保留用于误差溯源）
    scale: int               # 定点标度
    scaled_exact: Fraction   # original * scale 的精确值
    fixed: int               # 定点整数（最近舍入，平局向上，确定性）
    remainder: Fraction      # scaled_exact - fixed，落在 [-1/2, 1/2]
    direction: str           # "up" | "down" | "exact"

    def to_dict(self):
        return {
            "stage": self.stage,
            "original": self.original,
            "scale": self.scale,
            "fixed": self.fixed,
            "remainder": str(self.remainder),
            "remainder_float": float(self.remainder),
            "direction": self.direction,
        }


def to_fixed(value: float, scale: int, stage: str = "") -> ConversionRecord:
    """把浮点值按标度换算为定点整数，返回完整换算记录。"""
    if scale <= 0:
        raise ValueError("scale must be positive")
    scaled = Fraction(value) * scale
    floor = scaled.numerator // scaled.denominator
    frac = scaled - floor
    fixed = floor + 1 if frac >= Fraction(1, 2) else floor
    remainder = scaled - fixed
    if remainder == 0:
        direction = "exact"
    elif remainder < 0:
        direction = "up"
    else:
        direction = "down"
    return ConversionRecord(stage, float(value), scale, scaled, fixed, remainder, direction)


@dataclass
class AccumStep:
    stage: str
    position: int
    record: ConversionRecord

    def to_dict(self):
        return {
            "stage": self.stage,
            "position": self.position,
            "remainder": str(self.record.remainder),
            "remainder_float": float(self.record.remainder),
            "direction": self.record.direction,
            "fixed": self.record.fixed,
        }


@dataclass
class FixedAccumulator:
    """定点累加器：逐项保留舍入余量并累计误差上界。"""

    scale: int
    total_fixed: int = 0
    remainder_sum: Fraction = Fraction(0)   # 逐项余量之和（真值 = total_fixed + remainder_sum）
    error_bound: Fraction = Fraction(0)     # 累计误差上界（定点单位）
    steps: List[AccumStep] = field(default_factory=list)

    def add(self, record: ConversionRecord) -> int:
        position = len(self.steps)
        self.steps.append(AccumStep(record.stage, position, record))
        self.total_fixed += record.fixed
        self.remainder_sum += record.remainder
        self.error_bound += abs(record.remainder)
        return position

    @property
    def true_scaled(self) -> Fraction:
        """累加真值（定点单位），余量逐项参与。"""
        return Fraction(self.total_fixed) + self.remainder_sum

    def total_units(self) -> Fraction:
        return Fraction(self.total_fixed) / self.scale

    def error_bound_units(self) -> Fraction:
        return self.error_bound / self.scale

    def error_chain(self):
        return [step.to_dict() for step in self.steps]
