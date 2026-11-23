"""误差账本：记录定点换算的标度、舍入方向与被舍弃余量，累计加/乘误差上界。

所有选择率、偏差比与代价估计都以 fp_scale 标度的定点整数计算，
每一次换算在此登记一条账目，保证同一观测序列得到逐位一致的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LedgerEntry:
    context: str        # 观测点或计算位置标识，如 "obs:seq=3:step=0"
    op: str             # "to_fp" | "add" | "mul" | "div"
    scale: int
    rounding: str       # "floor" | "exact"
    discarded: int      # 被舍弃的余量（定点单位）
    error_bound: int    # 本步引入的误差上界（定点单位）


@dataclass
class ErrorLedger:
    scale: int
    entries: list[LedgerEntry] = field(default_factory=list)
    add_error_bound: int = 0
    mul_error_bound: int = 0

    def to_fp(self, numerator: int, denominator: int, context: str) -> int:
        """numerator / denominator 的定点换算，记录舍入方向与被舍弃余量。"""
        if denominator == 0:
            self.entries.append(LedgerEntry(context, "to_fp", self.scale, "exact", 0, 0))
            return 0
        scaled = numerator * self.scale
        quotient, remainder = divmod(scaled, denominator)
        rounding = "exact" if remainder == 0 else "floor"
        discarded = remainder // denominator if remainder else 0
        bound = 0 if remainder == 0 else 1
        self.entries.append(LedgerEntry(context, "to_fp", self.scale, rounding,
                                        discarded, bound))
        return quotient

    def record_add(self, context: str, operand_bounds: list[int]) -> int:
        """加法误差上界累计：和的误差上界为各加数误差上界之和。"""
        bound = sum(operand_bounds)
        self.add_error_bound += bound
        self.entries.append(LedgerEntry(context, "add", self.scale, "exact", 0, bound))
        return bound

    def record_mul(self, context: str, left: int, right: int,
                   left_bound: int, right_bound: int) -> int:
        """乘法误差上界：|ab - a'b'| <= a*eb + b*ea + ea*eb（定点单位）。"""
        bound = left * right_bound + right * left_bound + left_bound * right_bound
        self.mul_error_bound += bound
        self.entries.append(LedgerEntry(context, "mul", self.scale, "exact", 0, bound))
        return bound

    @property
    def total_error_bound(self) -> int:
        return self.add_error_bound + self.mul_error_bound
