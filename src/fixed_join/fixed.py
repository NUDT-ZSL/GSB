from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import EngineError


MAX_SCALE_EXPONENT = 9
MAX_SCALE_FACTOR = 10**MAX_SCALE_EXPONENT


@dataclass(frozen=True)
class FixedContext:
    scale_factor: int
    scale_exponent: int

    def quantize_float(self, value: float) -> int:
        if not math.isfinite(value):
            raise EngineError("E_TABLE", "floating value must be finite")
        numerator, denominator = value.as_integer_ratio()
        return (self.scale_factor * numerator) // denominator

    def to_real(self, lattice_value: int) -> float:
        return lattice_value / self.scale_factor

    def quantize_integer(self, value: int) -> int:
        return value


def parse_scale_factor(raw: Any) -> FixedContext:
    if isinstance(raw, bool):
        raise EngineError("E_SCALEF", "scale factor must be an integer power of ten")
    if isinstance(raw, int):
        factor = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or not text.isdigit():
            raise EngineError(
                "E_SCALEF",
                "scale factor must be a positive decimal integer power of ten",
                details={"received": raw},
            )
        factor = int(text)
    else:
        raise EngineError(
            "E_SCALEF",
            "scale factor must be a positive integer power of ten",
            details={"received_type": type(raw).__name__, "received": str(raw)},
        )

    if factor <= 0:
        raise EngineError(
            "E_SCALEF",
            "scale factor must be positive",
            details={"received": factor},
        )
    if factor > MAX_SCALE_FACTOR:
        raise EngineError(
            "E_SCALEF",
            "scale factor exceeds the supported maximum",
            details={"received": factor, "maximum": MAX_SCALE_FACTOR},
        )

    exponent = 0
    probe = factor
    while probe % 10 == 0:
        probe //= 10
        exponent += 1
    if probe != 1:
        raise EngineError(
            "E_SCALEF",
            "scale factor must be an exact power of ten",
            details={"received": factor},
        )
    return FixedContext(scale_factor=factor, scale_exponent=exponent)


def fixed_comparison(left: int, op: str, right: int) -> bool:
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(f"unsupported comparison operator: {op}")


def reference_comparison(left: int | float, op: str, right: int | float) -> bool:
    return fixed_comparison(left, op, right)
