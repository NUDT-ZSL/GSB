from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

import numpy as np

from .jsonutil import decimal_string, fixed_string, float_string


DECIMAL_PRECISION = 80


@dataclass(frozen=True)
class DualNumber:
    exact: Decimal
    fixed_scaled: int
    fixed_bound: Decimal
    float_value: np.float64
    float_bound: Decimal
    sources: frozenset[str]

@dataclass
class DualBuilder:
    metric: str
    scale: int
    rounding: str = "nearest_even"
    fixed_operations: list[dict[str, Any]] = field(default_factory=list)
    float_operations: list[dict[str, Any]] = field(default_factory=list)
    _sequence: int = 0

    def _next_id(self, operation: str) -> str:
        self._sequence += 1
        return f"{self.metric}:{self._sequence:04d}:{operation}"

    def _fixed_quantize(self, value: Decimal) -> tuple[int, Decimal]:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            scaled = value * self.scale
            quantized = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
            represented = Decimal(quantized) / Decimal(self.scale)
            error = abs(represented - value)
        return quantized, error

    def constant(self, value: Decimal, source: str, label: str) -> DualNumber:
        fixed_scaled, fixed_error = self._fixed_quantize(value)
        float_value = np.float64(float(value))
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            float_error = abs(Decimal.from_float(float(float_value)) - value)
        operation_id = self._next_id("const")
        self.fixed_operations.append(
            {
                "operation_id": operation_id,
                "operation": "constant",
                "label": label,
                "source": source,
                "raw": decimal_string(value),
                "fixed_scaled": str(fixed_scaled),
                "scale": self.scale,
                "rounding_direction": self.rounding,
                "added_error_bound": decimal_string(fixed_error),
                "cumulative_error_bound": decimal_string(fixed_error),
            }
        )
        self.float_operations.append(
            {
                "operation_id": operation_id,
                "operation": "constant",
                "label": label,
                "source": source,
                "raw": decimal_string(value),
                "float64": float_string(float_value),
                "float64_hex": float(float_value).hex(),
                "rounding_direction": "nearest_even",
                "added_error_bound": decimal_string(float_error),
                "cumulative_error_bound": decimal_string(float_error),
            }
        )
        return DualNumber(
            exact=value,
            fixed_scaled=fixed_scaled,
            fixed_bound=fixed_error,
            float_value=float_value,
            float_bound=float_error,
            sources=frozenset((source,)),
        )

    def integer(self, value: int, source: str, label: str) -> DualNumber:
        operation_id = self._next_id("integer")
        self.fixed_operations.append(
            {
                "operation_id": operation_id,
                "operation": "integer",
                "label": label,
                "source": source,
                "raw": str(value),
                "fixed_scaled": str(value * self.scale),
                "scale": self.scale,
                "rounding_direction": "exact",
                "added_error_bound": "0",
                "cumulative_error_bound": "0",
            }
        )
        self.float_operations.append(
            {
                "operation_id": operation_id,
                "operation": "integer",
                "label": label,
                "source": source,
                "raw": str(value),
                "float64": float_string(float(value)),
                "float64_hex": float(value).hex(),
                "rounding_direction": "exact",
                "added_error_bound": "0",
                "cumulative_error_bound": "0",
            }
        )
        return DualNumber(
            exact=Decimal(value),
            fixed_scaled=value * self.scale,
            fixed_bound=Decimal(0),
            float_value=np.float64(value),
            float_bound=Decimal(0),
            sources=frozenset((source,)),
        )

    def add(self, left: DualNumber, right: DualNumber, label: str) -> DualNumber:
        operation_id = self._next_id("add")
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            exact = left.exact + right.exact
            fixed_bound = left.fixed_bound + right.fixed_bound
            left_float = Decimal.from_float(float(left.float_value))
            right_float = Decimal.from_float(float(right.float_value))
            exact_float_operands = left_float + right_float
            result_float = np.float64(left.float_value + right.float_value)
            float_rounding_error = abs(
                Decimal.from_float(float(result_float)) - exact_float_operands
            )
            float_bound = left.float_bound + right.float_bound + float_rounding_error
        sources = left.sources | right.sources
        source_text = ",".join(sorted(sources))
        self.fixed_operations.append(
            {
                "operation_id": operation_id,
                "operation": "add",
                "label": label,
                "source": source_text,
                "left_scaled": str(left.fixed_scaled),
                "right_scaled": str(right.fixed_scaled),
                "result_scaled": str(left.fixed_scaled + right.fixed_scaled),
                "rounding_direction": "exact",
                "added_error_bound": "0",
                "cumulative_error_bound": decimal_string(fixed_bound),
            }
        )
        self.float_operations.append(
            {
                "operation_id": operation_id,
                "operation": "add",
                "label": label,
                "source": source_text,
                "left_float64": float_string(float(left.float_value)),
                "right_float64": float_string(float(right.float_value)),
                "result_float64": float_string(float(result_float)),
                "rounding_direction": "nearest_even",
                "added_error_bound": decimal_string(float_rounding_error),
                "cumulative_error_bound": decimal_string(float_bound),
            }
        )
        return DualNumber(
            exact=exact,
            fixed_scaled=left.fixed_scaled + right.fixed_scaled,
            fixed_bound=fixed_bound,
            float_value=result_float,
            float_bound=float_bound,
            sources=sources,
        )

    def multiply(self, left: DualNumber, right: DualNumber, label: str) -> DualNumber:
        operation_id = self._next_id("multiply")
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            exact = left.exact * right.exact
            bar = Decimal(left.fixed_scaled) / Decimal(self.scale)
            bbr = Decimal(right.fixed_scaled) / Decimal(self.scale)
            exact_fixed_operands = bar * bbr
            scaled_product = (
                Decimal(left.fixed_scaled) * Decimal(right.fixed_scaled) / Decimal(self.scale)
            )
            result_scaled = int(
                scaled_product.to_integral_value(rounding=ROUND_HALF_EVEN)
            )
            represented_fixed = Decimal(result_scaled) / Decimal(self.scale)
            fixed_rounding_error = abs(represented_fixed - exact_fixed_operands)
            fixed_propagated = (
                left.fixed_bound * abs(bbr)
                + right.fixed_bound * abs(bar)
                + left.fixed_bound * right.fixed_bound
            )
            fixed_bound = fixed_propagated + fixed_rounding_error

            left_float = Decimal.from_float(float(left.float_value))
            right_float = Decimal.from_float(float(right.float_value))
            exact_float_operands = left_float * right_float
            result_float = np.float64(left.float_value * right.float_value)
            float_rounding_error = abs(
                Decimal.from_float(float(result_float)) - exact_float_operands
            )
            float_propagated = (
                left.float_bound * abs(right_float)
                + right.float_bound * abs(left_float)
                + left.float_bound * right.float_bound
            )
            float_bound = float_propagated + float_rounding_error
        sources = left.sources | right.sources
        source_text = ",".join(sorted(sources))
        self.fixed_operations.append(
            {
                "operation_id": operation_id,
                "operation": "multiply",
                "label": label,
                "source": source_text,
                "left_scaled": str(left.fixed_scaled),
                "right_scaled": str(right.fixed_scaled),
                "result_scaled": str(result_scaled),
                "rounding_direction": self.rounding,
                "added_error_bound": decimal_string(fixed_rounding_error),
                "propagated_error_bound": decimal_string(fixed_propagated),
                "cumulative_error_bound": decimal_string(fixed_bound),
            }
        )
        self.float_operations.append(
            {
                "operation_id": operation_id,
                "operation": "multiply",
                "label": label,
                "source": source_text,
                "left_float64": float_string(float(left.float_value)),
                "right_float64": float_string(float(right.float_value)),
                "result_float64": float_string(float(result_float)),
                "rounding_direction": "nearest_even",
                "added_error_bound": decimal_string(float_rounding_error),
                "propagated_error_bound": decimal_string(float_propagated),
                "cumulative_error_bound": decimal_string(float_bound),
            }
        )
        return DualNumber(
            exact=exact,
            fixed_scaled=result_scaled,
            fixed_bound=fixed_bound,
            float_value=result_float,
            float_bound=float_bound,
            sources=sources,
        )

    def finalize(self, value: DualNumber) -> dict[str, Any]:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            fixed_real = Decimal(value.fixed_scaled) / Decimal(self.scale)
            combined_bound = max(value.fixed_bound, value.float_bound)
            fixed_actual_error = abs(fixed_real - value.exact)
            float_actual_error = abs(
                Decimal.from_float(float(value.float_value)) - value.exact
            )
        return {
            "fixed": {
                "scaled_string": str(value.fixed_scaled),
                "value": fixed_string(value.fixed_scaled, self.scale),
                "scale": self.scale,
                "rounding_direction": self.rounding,
                "pre_conversion_raw": decimal_string(value.exact),
                "cumulative_error_bound": decimal_string(value.fixed_bound),
                "actual_error_to_pre_conversion_raw": decimal_string(fixed_actual_error),
                "bound_is_upper": value.fixed_bound >= fixed_actual_error,
                "operations": self.fixed_operations,
            },
            "float64": {
                "value": float_string(float(value.float_value)),
                "bits_hex": float(value.float_value).hex(),
                "rounding_direction": "nearest_even",
                "pre_conversion_raw": decimal_string(value.exact),
                "cumulative_error_bound": decimal_string(value.float_bound),
                "actual_error_to_pre_conversion_raw": decimal_string(float_actual_error),
                "bound_is_upper": value.float_bound >= float_actual_error,
                "operations": self.float_operations,
            },
            "cumulative_error_bound": decimal_string(combined_bound),
            "bound_basis": "max(fixed,float64)",
            "propagation_sources": sorted(value.sources),
        }
