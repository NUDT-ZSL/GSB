"""整数定点基础设施。

本模块把比例、速率、权重统一量化到以 10 的整数幂为缩放因子的整数格上，
核心判定路径只允许使用 ``int`` 运算，禁止浮点中间值。

截断约定
--------
* 量化 :func:`quantize` 采用**向下截断**（floor），误差方向为「不高估」，
  量化余数 ``remainder`` 一并返回用于推导误差界。
* 定点乘法 :func:`mul_fixed` 先做整数乘积再整除缩放因子（floor），
  单次截断误差上界恒为 1 ulp（1 个整数格）。
* 定点除法 :func:`div_fixed` 先放大分子再整除（floor），
  单次截断误差上界恒为 1 ulp。
* 多级运算的误差界按级数线性合成：第 m 级总上界 <= m 个 ulp 的组合，
  具体每级贡献见 :class:`ErrorTrace`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import E_GRID, GovernanceError, Issue

MAX_EXPONENT = 12


@dataclass(frozen=True)
class Grid:
    """定点网格：scale = 10 ** exponent，1 ulp = 1 / scale。"""

    exponent: int
    scale: int

    @classmethod
    def from_exponent(cls, exponent: int) -> "Grid":
        if not isinstance(exponent, int) or isinstance(exponent, bool):
            raise GovernanceError(
                E_GRID,
                "缩放指数必须为整数",
                [Issue("grid.exponent", "期望 int 类型", value=type(exponent).__name__)],
            )
        if exponent < 0 or exponent > MAX_EXPONENT:
            raise GovernanceError(
                E_GRID,
                f"缩放指数必须落在 [0, {MAX_EXPONENT}]",
                [Issue("grid.exponent", "指数越界", value=exponent)],
            )
        return cls(exponent=exponent, scale=10**exponent)

    def to_dict(self) -> dict[str, int]:
        return {"exponent": self.exponent, "scale": self.scale}


@dataclass(frozen=True)
class Quantized:
    """量化结果：定点整数、余数（同分母单位）与误差上界（ulp 数）。"""

    value: int
    remainder: int
    grid: Grid

    @property
    def error_ulps(self) -> int:
        """floor 量化误差严格小于 1 ulp，报告上界取 1 ulp。"""
        return 1

    def to_dict(self) -> dict[str, int]:
        return {
            "value": self.value,
            "remainder": self.remainder,
            "error_bound_ulp": self.error_ulps,
        }


def quantize(numerator: int, denominator: int, grid: Grid) -> Quantized:
    """把有理数 ``numerator / denominator`` 量化到网格（floor 截断）。

    返回定点整数 ``floor(numerator*scale/denominator)`` 与余数，
    满足 ``0 <= 真值 - 量化值/scale < 1/scale``。
    """
    if not isinstance(numerator, int) or isinstance(numerator, bool):
        raise GovernanceError(E_GRID, "量化分子必须为整数",
                              [Issue("fixedpoint.numerator", "期望 int", value=type(numerator).__name__)])
    if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator <= 0:
        raise GovernanceError(E_GRID, "量化分母必须为正整数",
                              [Issue("fixedpoint.denominator", "期望正 int", value=denominator)])
    scaled = numerator * grid.scale
    value, remainder = divmod(scaled, denominator)
    return Quantized(value=value, remainder=remainder, grid=grid)


def mul_fixed(a: int, b: int, grid: Grid) -> tuple[int, int]:
    """两个同网格定点数相乘，返回 ``(定点积, 截断余数)``，floor 截断。"""
    if not isinstance(a, int) or not isinstance(b, int):
        raise GovernanceError(E_GRID, "定点乘法操作数必须为 int",
                              [Issue("fixedpoint.mul", "期望 int 操作数")])
    product = a * b
    value, remainder = divmod(product, grid.scale)
    return value, remainder


def div_fixed(numerator: int, denominator: int, grid: Grid) -> tuple[int, int]:
    """定点除法 ``numerator / denominator``（两者可为普通整数比），floor 截断。"""
    if not isinstance(denominator, int) or denominator <= 0:
        raise GovernanceError(E_GRID, "定点除法分母必须为正整数",
                              [Issue("fixedpoint.div", "分母非正", value=denominator)])
    value, remainder = divmod(numerator * grid.scale, denominator)
    return value, remainder


def compare_percent(numerator: int, denominator: int, threshold_percent: int) -> bool:
    """无浮点比较 ``numerator/denominator >= threshold_percent/100``。

    交叉相乘恒为精确整数比较，比较环节本身误差为 0；
    定点失败率的展示误差由调用方通过 :func:`quantize` 单独报告。
    """
    return numerator * 100 >= threshold_percent * denominator


def format_fixed(value: int, grid: Grid) -> str:
    """把定点整数渲染为十进制字符串（不使用浮点）。"""
    if grid.exponent == 0:
        return str(value)
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    whole, frac = divmod(absolute, grid.scale)
    return f"{sign}{whole}.{str(frac).zfill(grid.exponent)}"


@dataclass
class LevelContribution:
    """降级链单级误差贡献记录。"""

    level: int
    target: str
    success_fixed: int
    input_quant_ulp: int
    truncation_ulp: int
    cumulative_value: int
    cumulative_error_ulp: int
    cumulative_remainder: int

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "target": self.target,
            "success_fixed": self.success_fixed,
            "input_quantization_error_ulp": self.input_quant_ulp,
            "truncation_error_ulp": self.truncation_ulp,
            "cumulative_value": self.cumulative_value,
            "cumulative_error_bound_ulp": self.cumulative_error_ulp,
            "cumulative_remainder": self.cumulative_remainder,
        }


@dataclass
class ErrorTrace:
    """逐级误差界合成轨迹（单位均为 ulp，即整数格）。"""

    grid: Grid
    levels: list[LevelContribution] = field(default_factory=list)

    @property
    def total_error_ulp(self) -> int:
        if not self.levels:
            return 0
        return self.levels[-1].cumulative_error_ulp

    def to_dict(self) -> dict[str, object]:
        return {
            "grid": self.grid.to_dict(),
            "total_error_bound_ulp": self.total_error_ulp,
            "levels": [level.to_dict() for level in self.levels],
        }


def compose_chain_probability(
    steps: list[tuple[str, int, int]],
    grid: Grid,
) -> tuple[int, ErrorTrace]:
    """沿降级链合成端到端成功概率。

    ``steps`` 为 ``(目标, 该级成功概率定点值, 输入量化误差 ulp)`` 的有序列表，
    初始值为 1.0 的定点表示 ``scale``。每级做一次定点乘法，
    保守误差递推::

        e_0 = 0
        e_m = e_{m-1} + q_m + t_m

    其中 ``q_m`` 为该级输入量化误差上界（已知定点值取 0，
    现场由计数统计量化取 1，配置直接给出的定点值取 0），
    ``t_m=1`` 为乘法 floor 截断上界。
    """
    trace = ErrorTrace(grid=grid)
    cumulative = grid.scale
    cumulative_error = 0
    for index, (target, success_fixed, input_quant) in enumerate(steps, start=1):
        new_value, remainder = mul_fixed(cumulative, success_fixed, grid)
        truncation = 1
        cumulative_error = cumulative_error + input_quant + truncation
        cumulative = new_value
        trace.levels.append(
            LevelContribution(
                level=index,
                target=target,
                success_fixed=success_fixed,
                input_quant_ulp=input_quant,
                truncation_ulp=truncation,
                cumulative_value=cumulative,
                cumulative_error_ulp=cumulative_error,
                cumulative_remainder=remainder,
            )
        )
    return cumulative, trace
