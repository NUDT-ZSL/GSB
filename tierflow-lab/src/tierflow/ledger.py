from __future__ import annotations

from fractions import Fraction
from typing import Any


def fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


class FixedCostLedger:
    """Exact fixed-point cost ledger with conservative error bounds."""

    def __init__(self, object_key: str, scale: int = 1_000_000) -> None:
        if scale <= 0:
            raise ValueError("fixed point scale must be positive")
        self.object_key = object_key
        self.scale = scale
        self.value = 0
        self.error_bound = Fraction(0, 1)
        self.entries: list[dict[str, Any]] = []

    def _append(
        self,
        *,
        operation: str,
        input_raw: str,
        raw_value: Fraction,
        rounded_value: int,
        direction: str,
        rounding_bound: Fraction,
        propagated_bound: Fraction,
        formula: str,
    ) -> None:
        remainder = raw_value - rounded_value
        self.error_bound = propagated_bound + rounding_bound
        self.value = rounded_value
        self.entries.append(
            {
                "seq": len(self.entries),
                "operation": operation,
                "formula": formula,
                "input_raw": input_raw,
                "raw_value": fraction_text(raw_value),
                "scale": self.scale,
                "rounded_value_scaled": rounded_value,
                "rounding_direction": direction,
                "rounding_remainder_scaled": fraction_text(remainder),
                "rounding_error_bound_scaled": fraction_text(rounding_bound),
                "propagated_error_bound_scaled": fraction_text(propagated_bound),
                "cumulative_error_bound_scaled": fraction_text(self.error_bound),
                "cumulative_value_scaled": self.value,
            }
        )

    def convert_bytes_to_time(self, size_bytes: int, bandwidth_bytes_per_ms: int) -> None:
        if bandwidth_bytes_per_ms <= 0:
            raise ValueError("bandwidth must be positive")
        raw = Fraction(size_bytes * self.scale, bandwidth_bytes_per_ms)
        rounded = raw.numerator // raw.denominator
        self._append(
            operation="byte_conversion",
            input_raw=f"{size_bytes} bytes; bandwidth={bandwidth_bytes_per_ms} bytes/ms",
            raw_value=raw,
            rounded_value=rounded,
            direction="floor",
            rounding_bound=Fraction(1, 1),
            propagated_bound=self.error_bound,
            formula="floor(size_bytes * scale / bandwidth_bytes_per_ms)",
        )

    def multiply_rational(self, numerator: int, denominator: int, label: str) -> None:
        if denominator <= 0:
            raise ValueError("multiplier denominator must be positive")
        before_value = self.value
        before_error = self.error_bound
        raw = Fraction(before_value * numerator, denominator)
        rounded = raw.numerator // raw.denominator
        propagated = before_error * Fraction(abs(numerator), denominator)
        self._append(
            operation="multiply",
            input_raw=f"{label}: {before_value} * {numerator}/{denominator}",
            raw_value=raw,
            rounded_value=rounded,
            direction="floor",
            rounding_bound=Fraction(1, 1),
            propagated_bound=propagated,
            formula=f"floor(value * {numerator}/{denominator}); prior_error * |{numerator}/{denominator}|",
        )

    def add_scaled(self, amount: int, label: str, amount_error: Fraction = Fraction(0, 1)) -> None:
        before_error = self.error_bound
        raw = Fraction(self.value + amount, 1)
        rounded = self.value + amount
        self.value = rounded
        self.error_bound = before_error + amount_error
        self.entries.append(
            {
                "seq": len(self.entries),
                "operation": "add",
                "formula": "value + overhead",
                "input_raw": f"{label}: {amount} scaled units",
                "raw_value": fraction_text(raw),
                "scale": self.scale,
                "rounded_value_scaled": rounded,
                "rounding_direction": "exact",
                "rounding_remainder_scaled": "0/1",
                "rounding_error_bound_scaled": "0/1",
                "propagated_error_bound_scaled": fraction_text(before_error + amount_error),
                "cumulative_error_bound_scaled": fraction_text(self.error_bound),
                "cumulative_value_scaled": self.value,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_key": self.object_key,
            "scale": self.scale,
            "total_cost_scaled": self.value,
            "error_bound_scaled": fraction_text(self.error_bound),
            "error_bound_numerator": self.error_bound.numerator,
            "error_bound_denominator": self.error_bound.denominator,
            "error_bound_decimal": float(self.error_bound),
            "error_source_chain": [
                {
                    "seq": entry["seq"],
                    "operation": entry["operation"],
                    "cumulative_error_bound_scaled": entry["cumulative_error_bound_scaled"],
                    "rounding_direction": entry["rounding_direction"],
                    "rounding_remainder_scaled": entry["rounding_remainder_scaled"],
                }
                for entry in self.entries
            ],
            "entries": self.entries,
        }


def build_object_ledger(obj: dict[str, Any], cost_config: dict[str, Any]) -> dict[str, Any]:
    scale = int(cost_config.get("fixed_point_scale", 1_000_000))
    ledger = FixedCostLedger(obj["key"], scale)
    ledger.convert_bytes_to_time(
        int(obj["size_bytes"]),
        int(cost_config["bandwidth_bytes_per_ms"]),
    )
    ledger.multiply_rational(
        int(cost_config.get("complexity_multiplier_num", 100)),
        int(cost_config.get("complexity_multiplier_den", 100)),
        "heat_complexity",
    )
    overhead = int(cost_config.get("fixed_overhead_scaled", 0))
    overhead_error = Fraction(1, 2) if cost_config.get("overhead_is_rounded", False) else Fraction(0)
    ledger.add_scaled(overhead, "fixed_open_and_verify_overhead", overhead_error)
    result = ledger.to_dict()
    result["raw_inputs"] = {
        "size_bytes": int(obj["size_bytes"]),
        "bandwidth_bytes_per_ms": int(cost_config["bandwidth_bytes_per_ms"]),
        "complexity_multiplier_num": int(cost_config.get("complexity_multiplier_num", 100)),
        "complexity_multiplier_den": int(cost_config.get("complexity_multiplier_den", 100)),
        "fixed_overhead_scaled": overhead,
        "declared_scale": scale,
    }
    return result


def compare_costs(
    ledger_a: dict[str, Any],
    ledger_b: dict[str, Any],
    tolerance_scaled: int,
) -> dict[str, Any]:
    cost_a = int(ledger_a["total_cost_scaled"])
    cost_b = int(ledger_b["total_cost_scaled"])
    diff = abs(cost_a - cost_b)
    error_a = Fraction(
        int(ledger_a["error_bound_numerator"]),
        int(ledger_a["error_bound_denominator"]),
    )
    error_b = Fraction(
        int(ledger_b["error_bound_numerator"]),
        int(ledger_b["error_bound_denominator"]),
    )
    error_sum = error_a + error_b
    return {
        "object_a": ledger_a["object_key"],
        "object_b": ledger_b["object_key"],
        "cost_a_scaled": cost_a,
        "cost_b_scaled": cost_b,
        "cost_difference_scaled": diff,
        "tolerance_scaled": tolerance_scaled,
        "within_tolerance": diff < tolerance_scaled,
        "combined_error_bound_scaled": fraction_text(error_sum),
        "difference_within_error_bounds": Fraction(diff, 1) <= error_sum,
        "precision_sensitive": diff < tolerance_scaled,
        "error_source_chain_a": ledger_a["error_source_chain"],
        "error_source_chain_b": ledger_b["error_source_chain"],
    }
