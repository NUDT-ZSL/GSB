"""Shared arithmetic semantics for float64, float32, and scaled integers."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import BusinessError, E_CONFIG_FIXED_SCALE, E_CONFIG_PRECISION, E_CONFIG_ROUNDING


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def _round_ratio(numerator: int, denominator: int, mode: str) -> int:
    """Round an exact integer ratio using an explicit mode."""

    if denominator == 0:
        raise ZeroDivisionError("integer denominator is zero")
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    twice = remainder * 2

    if mode == "toward_zero":
        increment = False
    elif mode == "floor":
        increment = sign < 0 and remainder != 0
    elif mode == "ceil":
        increment = sign > 0 and remainder != 0
    elif mode == "half_up":
        increment = twice >= denominator
    elif mode == "half_even":
        if twice > denominator:
            increment = True
        elif twice < denominator:
            increment = False
        else:
            increment = quotient % 2 == 1
    else:
        raise BusinessError(
            E_CONFIG_ROUNDING,
            f"unknown fixed-point rounding mode: {mode}",
            field="precision.rounding",
            stage="precision",
        )
    return sign * (quotient + (1 if increment else 0))


def _round_float_to_int(value: float, mode: str) -> int:
    if mode == "toward_zero":
        return int(value)
    if mode == "floor":
        return math.floor(value)
    if mode == "ceil":
        return math.ceil(value)
    if mode == "half_up":
        return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)
    if mode == "half_even":
        return int(round(value))
    raise BusinessError(
        E_CONFIG_ROUNDING,
        f"unknown fixed-point rounding mode: {mode}",
        field="precision.rounding",
        stage="precision",
    )


class PrecisionOps:
    """Precision-aware operator layer with one semantic surface."""

    def __init__(self, name: str, precision_config: dict[str, Any]) -> None:
        if name not in ("float64", "float32", "fixed"):
            raise BusinessError(
                E_CONFIG_PRECISION,
                f"unknown precision profile: {name}",
                field="precision.active",
                stage="precision",
            )
        self.name = name
        self.config = precision_config
        self.scale = int(precision_config["fixed_scale"])
        self.rounding = str(precision_config["rounding"])
        self.subnormal_floor = float(precision_config["subnormal_floor"])
        self.subnormal_protection = bool(precision_config["subnormal_protection"])
        if name == "fixed":
            if self.scale <= 0:
                raise BusinessError(
                    E_CONFIG_FIXED_SCALE,
                    "fixed scale must be positive",
                    field="precision.fixed_scale",
                    stage="precision",
                )
            bits = self.scale.bit_length() - 1
            self.fractional_bits = max(0, bits)
            self.lane_bits = 64
            self.product_bits = 128
            self.dtype = None
        elif name == "float32":
            self.fractional_bits = None
            self.lane_bits = 32
            self.product_bits = 32
            self.dtype = np.float32
        else:
            self.fractional_bits = None
            self.lane_bits = 64
            self.product_bits = 64
            self.dtype = np.float64
        self._stage = "unscoped"
        self._stage_start = len(self._events)

    @property
    def _events(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_event_list"):
            self._event_list: list[dict[str, Any]] = []
        return self._event_list

    def begin_stage(self, stage: str) -> None:
        self._stage = stage
        self._stage_start = len(self._events)

    def _event(self, kind: str, **values: Any) -> None:
        self._events.append({"kind": kind, "stage": self._stage, **values})

    def _protect_float(self, value: float) -> float:
        if self.subnormal_protection and value != 0.0 and abs(value) < self.subnormal_floor:
            self._event("subnormal_clamp", from_value=value, to_value=math.copysign(self.subnormal_floor, value))
            return math.copysign(self.subnormal_floor, value)
        return value

    def q(self, value: Any) -> Any:
        """Quantize a Python or NumPy scalar to the active storage domain."""

        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"non-finite value in {self.name}: {number!r}")
        if self.name == "fixed":
            raw = _round_float_to_int(number * self.scale, self.rounding)
            if number != 0.0 and raw == 0 and self.subnormal_protection:
                raw = 1 if number > 0 else -1
                self._event("fixed_underflow_clamp", from_value=number, to_value=raw)
            self._event("quantize", value=number, raw=raw)
            return self._saturate(raw)
        quantized = self.dtype(number)
        protected = self._protect_float(float(quantized))
        self._event("quantize", value=number, raw=float(protected))
        return self.dtype(protected)

    def _saturate(self, raw: int) -> int:
        if raw > INT64_MAX:
            self._event("saturate", direction="high", raw=raw)
            return INT64_MAX
        if raw < INT64_MIN:
            self._event("saturate", direction="low", raw=raw)
            return INT64_MIN
        return raw

    def add(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            return self._saturate(int(left) + int(right))
        return self.dtype(self.dtype(left) + self.dtype(right))

    def sub(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            return self._saturate(int(left) - int(right))
        return self.dtype(self.dtype(left) - self.dtype(right))

    def mul(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            return self._saturate(_round_ratio(int(left) * int(right), self.scale, self.rounding))
        return self.dtype(self.dtype(left) * self.dtype(right))

    def div(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            if int(right) == 0:
                self._event("division_guard", left=int(left))
                return 0
            return self._saturate(_round_ratio(int(left) * self.scale, int(right), self.rounding))
        denominator = self.dtype(right)
        if float(denominator) == 0.0:
            self._event("division_guard", left=float(left))
            return self.dtype(0.0)
        return self.dtype(self.dtype(left) / denominator)

    def abs(self, value: Any) -> Any:
        if self.name == "fixed":
            return self._saturate(abs(int(value)))
        return self.dtype(abs(self.dtype(value)))

    def neg(self, value: Any) -> Any:
        if self.name == "fixed":
            return self._saturate(-int(value))
        return self.dtype(-self.dtype(value))

    def maximum(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            return max(int(left), int(right))
        return self.dtype(max(float(self.dtype(left)), float(self.dtype(right))))

    def minimum(self, left: Any, right: Any) -> Any:
        if self.name == "fixed":
            return min(int(left), int(right))
        return self.dtype(min(float(self.dtype(left)), float(self.dtype(right))))

    def to_float(self, value: Any) -> float:
        if self.name == "fixed":
            return float(int(value)) / float(self.scale)
        return float(self.dtype(value))

    def raw(self, value: Any) -> int | None:
        return int(value) if self.name == "fixed" else None

    def storage(self, value: Any) -> dict[str, Any]:
        if self.name == "fixed":
            raw = int(value)
            return {
                "storage": "scaled_int64",
                "raw": raw,
                "scale": self.scale,
                "value": raw / self.scale,
            }
        stored = self.q(value)
        return {
            "storage": "ieee754",
            "bits": self.lane_bits,
            "value": float(stored),
        }

    def subnormal_hit(self, value: Any) -> bool:
        if not self.subnormal_protection:
            return False
        number = abs(self.to_float(value))
        return 0.0 < number < self.subnormal_floor

    def stage_record(self, stage: str) -> dict[str, Any]:
        events = self._events[self._stage_start :]
        quantizations = sum(1 for event in events if event["kind"] == "quantize")
        subnormal_clamps = sum(1 for event in events if event["kind"] in ("subnormal_clamp", "fixed_underflow_clamp"))
        saturations = sum(1 for event in events if event["kind"] == "saturate")
        guards = sum(1 for event in events if event["kind"] == "division_guard")
        record: dict[str, Any] = {
            "stage": stage,
            "precision": self.name,
            "lane_bits": self.lane_bits,
            "product_bits": self.product_bits,
            "storage": "scaled_int64" if self.name == "fixed" else "ieee754",
            "scale": self.scale if self.name == "fixed" else None,
            "fractional_bits": self.fractional_bits,
            "rounding": self.rounding if self.name == "fixed" else "ieee754_round_to_nearest_even",
            "quantizations": quantizations,
            "subnormal_clamps": subnormal_clamps,
            "saturations": saturations,
            "division_guards": guards,
        }
        if self.name == "fixed":
            raw_values = [event["raw"] for event in events if event["kind"] == "quantize"]
            record["observed_raw_range"] = [min(raw_values), max(raw_values)] if raw_values else [0, 0]
        else:
            values = [event["raw"] for event in events if event["kind"] == "quantize"]
            record["observed_value_range"] = [min(values), max(values)] if values else [0.0, 0.0]
        return record

    def profile(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lane_bits": self.lane_bits,
            "product_bits": self.product_bits,
            "fractional_bits": self.fractional_bits,
            "scale": self.scale if self.name == "fixed" else None,
            "rounding": self.rounding if self.name == "fixed" else "ieee754_round_to_nearest_even",
            "subnormal_floor": self.subnormal_floor,
            "subnormal_protection": self.subnormal_protection,
        }


def compare_with_tolerance(
    value: float,
    threshold: float,
    strategy: str,
    tolerance: float,
) -> dict[str, Any]:
    """Stable threshold decision.

    The decision is always made on the float64 reference value. In tolerance mode,
    a value inside the closed band is accepted (tie-to-pass) and the protection is
    recorded. Therefore changing arithmetic precision cannot flip participation.
    """

    margin = value - threshold
    if strategy == "strict":
        decision = margin >= 0.0
        protected = False
    elif strategy == "tolerance":
        protected = abs(margin) <= tolerance
        decision = True if protected else margin > 0.0
    else:
        raise BusinessError(
            E_CONFIG_PRECISION,
            f"unknown comparison strategy: {strategy}",
            field="precision.compare_strategy",
            stage="threshold",
        )
    return {
        "strategy": strategy,
        "threshold": threshold,
        "tolerance": tolerance,
        "margin": margin,
        "decision": decision,
        "protection_engaged": protected,
        "decision_domain": "float64_reference",
    }

