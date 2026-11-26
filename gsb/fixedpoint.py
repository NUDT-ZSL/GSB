"""定点标度换算、舍入、余量结转与误差上界累计。

设计要点：

- 所有时间基准为逻辑毫秒（整数 ``int`` 或高精度 ``Decimal``）。
- 定点标度用 ``unit_ms`` 表示“一个定点单位等于多少毫秒”，允许大于 1
  （例如 1 单位 = 100ms），从而自然产生被舍弃的亚单位余量。
- :class:`RemainderCarry` 按实例保存未满一个定点单位的余量，下一步先把
  结转余量并入总额再重新取整，保证余量参与后续累加且不重复计入。
- :func:`derive_margin` 显式登记除法/乘法/加法/取整每一步的局部误差上界
  与累计误差上界，形成“误差来源链”。

全部使用 ``decimal.Decimal`` 与确定性舍入，不使用 float。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext
from typing import Any, Dict, List, Optional

getcontext().prec = 60

ROUNDING = {
    "ROUND_FLOOR": ROUND_FLOOR,
    "ROUND_CEILING": ROUND_CEILING,
    "ROUND_HALF_UP": ROUND_HALF_UP,
}


def D(value: Any) -> Decimal:
    """确定性地把 int/str/Decimal 转为 Decimal；拒绝 float 以免引入二进制误差。"""
    if isinstance(value, float):
        raise TypeError("float is not allowed in fixed-point arithmetic; use str/int/Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class Scale:
    """定点标度。``unit_ms`` 为一个定点单位对应的毫秒数。"""

    unit_ms: Decimal
    rounding: str = "ROUND_FLOOR"
    label: str = ""

    def __post_init__(self) -> None:  # dataclass frozen + 规范化
        if not isinstance(self.unit_ms, Decimal):
            object.__setattr__(self, "unit_ms", D(self.unit_ms))
        if self.rounding not in ROUNDING:
            raise ValueError(f"unsupported rounding: {self.rounding}")
        if self.unit_ms <= 0:
            raise ValueError("unit_ms must be positive")

    @classmethod
    def make(cls, unit_ms: Any, rounding: str = "ROUND_FLOOR", label: str = "") -> "Scale":
        return cls(unit_ms=D(unit_ms), rounding=rounding, label=label)

    def finer(self, factor: int = 1000) -> "Scale":
        """提高标度：单位缩小 ``factor`` 倍（小数位更多）。"""
        return Scale(
            unit_ms=self.unit_ms / Decimal(int(factor)),
            rounding=self.rounding,
            label=(self.label + f"+x{factor}").lstrip("+"),
        )

    def evidence(self) -> Dict[str, Any]:
        return {
            "unit_ms": str(self.unit_ms),
            "rounding": self.rounding,
            "scale_label": self.label or f"1_unit={self.unit_ms}ms",
        }


@dataclass
class QuantResult:
    units: int
    quantized_ms: Decimal
    remainder_ms: Decimal
    local_error_bound_ms: Decimal


def quantize_ms(raw_ms: Any, scale: Scale, rounding: Optional[str] = None) -> QuantResult:
    """按标度把毫秒值量化到整数个定点单位。

    返回量化单位数、量化后毫秒值、余量（raw - quantized；FLOOR 时落在
    ``[0, unit)``，CEIL 时落在 ``(-unit, 0]``）以及局部误差上界。
    """
    mode_name = rounding or scale.rounding
    mode = ROUNDING[mode_name]
    raw = D(raw_ms)
    unit = scale.unit_ms
    units_dec = (raw / unit).to_integral_value(rounding=mode)
    units = int(units_dec)
    quantized = Decimal(units) * unit
    remainder = raw - quantized
    if mode_name == "ROUND_HALF_UP":
        bound = unit / Decimal(2)
    else:
        bound = unit
    return QuantResult(units, quantized, remainder, bound)


@dataclass
class DelayAccumulator:
    """逐实例延迟累加器，结转未满一个定点单位的余量。"""

    instance_id: str
    scale: Scale
    cumulative_raw_ms: Decimal = Decimal(0)
    cumulative_units: int = 0
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, delta_ms: int, **labels: Any) -> Dict[str, Any]:
        delta = int(delta_ms)
        before_units = self.cumulative_units
        self.cumulative_raw_ms += Decimal(delta)
        qr = quantize_ms(self.cumulative_raw_ms, self.scale, "ROUND_FLOOR")
        self.cumulative_units = qr.units
        newly_recognized_units = qr.units - before_units
        step = {
            **labels,
            "delta_ms": delta,
            "scale": self.scale.evidence(),
            "newly_recognized_units": newly_recognized_units,
            "cumulative_units": qr.units,
            "cumulative_quantized_ms": str(qr.quantized_ms),
            "carried_remainder_ms": str(qr.remainder_ms),
        }
        self.steps.append(step)
        return step

    def result(self) -> Dict[str, Any]:
        qr = quantize_ms(self.cumulative_raw_ms, self.scale, "ROUND_FLOOR")
        return {
            "instance_id": self.instance_id,
            "scale": self.scale.evidence(),
            "delay_exact_ms": str(self.cumulative_raw_ms),
            "delay_fixed_units": qr.units,
            "delay_fixed_ms": str(qr.quantized_ms),
            "carried_remainder_ms": str(qr.remainder_ms),
            "rounding": "ROUND_FLOOR",
            "steps": self.steps,
        }


@dataclass
class MarginResult:
    backlog_bytes: int
    exact_ms: Decimal
    margin_ms: Decimal
    error_bound_ms: Decimal
    scale: Scale
    rate_bytes_per_ms: Decimal
    safety_factor: Decimal
    slack_ms: Decimal
    trace: List[Dict[str, Any]]

    def evidence(self) -> Dict[str, Any]:
        return {
            "backlog_bytes": self.backlog_bytes,
            "rate_bytes_per_ms": str(self.rate_bytes_per_ms),
            "safety_factor": str(self.safety_factor),
            "slack_ms": str(self.slack_ms),
            "scale": self.scale.evidence(),
            "exact_duration_ms": str(self.exact_ms),
            "margin_ms": str(self.margin_ms),
            "error_bound_ms": str(self.error_bound_ms),
            "error_source_chain": self.trace,
        }


def derive_margin(
    backlog_bytes: int,
    rate_bytes_per_ms: Any,
    safety_factor: Any,
    slack_ms: Any,
    scale: Scale,
) -> MarginResult:
    """未回放字节量 -> 最小追赶余量（逻辑毫秒），并累计误差上界。

    流水线（每步量化均采用保守的向上取整）::

        d1 = ceil_unit(bytes / rate)                 # 除法 + 取整
        d2 = ceil_unit(d1 * safety_factor)            # 乘法 + 误差传播/取整
        d3 = ceil_unit(d2 + slack_ms)                 # 加法 + 误差传播/取整

    累计误差上界 = unit*safety + 2*unit（传播 + 两次量化），逐步写入 trace。
    """
    backlog = int(backlog_bytes)
    rate = D(rate_bytes_per_ms)
    safety = D(safety_factor)
    slack = D(slack_ms)
    trace: List[Dict[str, Any]] = []

    exact = (Decimal(backlog) / rate) * safety + slack

    raw_div = Decimal(backlog) / rate
    q1 = quantize_ms(raw_div, scale, "ROUND_CEILING")
    bound1 = q1.local_error_bound_ms
    trace.append(
        {
            "step": "bytes_div_rate",
            "op": "div+ceil_quantize",
            "raw_ms": str(raw_div),
            "quantized_ms": str(q1.quantized_ms),
            "rounding": "ROUND_CEILING",
            "local_error_bound_ms": str(q1.local_error_bound_ms),
            "cumulative_error_bound_ms": str(bound1),
        }
    )

    raw_mul = q1.quantized_ms * safety
    q2 = quantize_ms(raw_mul, scale, "ROUND_CEILING")
    bound2 = bound1 * safety + q2.local_error_bound_ms
    trace.append(
        {
            "step": "multiply_safety_factor",
            "op": "mul+ceil_quantize",
            "operand_ms": str(q1.quantized_ms),
            "factor": str(safety),
            "raw_ms": str(raw_mul),
            "quantized_ms": str(q2.quantized_ms),
            "rounding": "ROUND_CEILING",
            "propagated_error_bound_ms": str(bound1 * safety),
            "local_error_bound_ms": str(q2.local_error_bound_ms),
            "cumulative_error_bound_ms": str(bound2),
        }
    )

    raw_add = q2.quantized_ms + slack
    q3 = quantize_ms(raw_add, scale, "ROUND_CEILING")
    bound3 = bound2 + q3.local_error_bound_ms
    trace.append(
        {
            "step": "add_checkpoint_slack",
            "op": "add+ceil_quantize",
            "operand_ms": str(q2.quantized_ms),
            "addend_ms": str(slack),
            "raw_ms": str(raw_add),
            "quantized_ms": str(q3.quantized_ms),
            "rounding": "ROUND_CEILING",
            "local_error_bound_ms": str(q3.local_error_bound_ms),
            "cumulative_error_bound_ms": str(bound3),
        }
    )

    return MarginResult(
        backlog_bytes=backlog,
        exact_ms=exact,
        margin_ms=q3.quantized_ms,
        error_bound_ms=bound3,
        scale=scale,
        rate_bytes_per_ms=rate,
        safety_factor=safety,
        slack_ms=slack,
        trace=trace,
    )


def convert_relative_seconds(
    now_ms: int, relative_seconds: int, scale: Scale
) -> Dict[str, Any]:
    """v1 相对保留秒数 -> 注入逻辑时钟下的绝对截止时刻，并登记换算标度。"""
    rel = int(relative_seconds)
    raw_deadline = Decimal(int(now_ms)) + Decimal(rel) * Decimal(1000)
    qr = quantize_ms(raw_deadline, scale, "ROUND_FLOOR")
    return {
        "kind": "relative_seconds",
        "relative_retention_seconds": rel,
        "base_clock_ms": int(now_ms),
        "absolute_deadline_ms": int(raw_deadline),
        "fixed_quantized_deadline_ms": str(qr.quantized_ms),
        "discarded_remainder_ms": str(qr.remainder_ms),
        "scale": scale.evidence(),
        "rounding": "ROUND_FLOOR",
    }
