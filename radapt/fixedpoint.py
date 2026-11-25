from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable


def frac_str(value: Fraction | None) -> str | None:
    if value is None:
        return None
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


@dataclass(frozen=True)
class FixedValue:
    subject: str
    kind: str
    exact: Fraction
    scale: int
    integer: int
    rounding: str
    discarded_remainder: Fraction
    error_bound: Fraction
    conversion_id: str
    source_batches: tuple[int, ...] = ()

    @property
    def represented(self) -> Fraction:
        return Fraction(self.integer, self.scale)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversion_id": self.conversion_id,
            "subject": self.subject,
            "kind": self.kind,
            "source_batches": list(self.source_batches),
            "exact_value": frac_str(self.exact),
            "scale": self.scale,
            "integer_value": self.integer,
            "rounding_direction": self.rounding,
            "discarded_remainder": frac_str(self.discarded_remainder),
            "represented_value": frac_str(self.represented),
            "error_bound": frac_str(self.error_bound),
        }


@dataclass
class ErrorLedger:
    scale: int
    default_rounding: str = "floor"
    conversions: list[FixedValue] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    cumulative_add_error_bound: Fraction = Fraction(0)
    cumulative_mul_error_bound: Fraction = Fraction(0)
    _counter: int = 0

    def convert(
        self,
        value: Fraction | int,
        *,
        subject: str,
        kind: str,
        source_batches: Iterable[int] = (),
        rounding: str | None = None,
    ) -> FixedValue:
        exact = Fraction(value)
        mode = rounding or self.default_rounding
        if mode not in {"floor", "ceil"}:
            raise ValueError(f"unsupported rounding mode: {mode}")
        scaled_exact = exact * self.scale
        floor_int = scaled_exact.numerator // scaled_exact.denominator
        fractional = scaled_exact - floor_int
        conversion_id = f"conv-{len(self.conversions) + 1:04d}"
        if fractional == 0:
            integer = floor_int
            direction = "exact"
            discarded = Fraction(0)
        elif mode == "floor":
            integer = floor_int
            direction = "floor_down"
            discarded = exact - Fraction(integer, self.scale)
        else:
            integer = floor_int + 1
            direction = "ceil_up"
            discarded = Fraction(integer, self.scale) - exact
        fixed = FixedValue(
            subject=subject,
            kind=kind,
            exact=exact,
            scale=self.scale,
            integer=integer,
            rounding=direction,
            discarded_remainder=abs(discarded),
            error_bound=Fraction(1, self.scale),
            conversion_id=conversion_id,
            source_batches=tuple(source_batches),
        )
        self.conversions.append(fixed)
        return fixed

    def add(self, values: Iterable[FixedValue], *, subject: str) -> FixedValue:
        values = tuple(values)
        if not values:
            raise ValueError("add requires at least one fixed value")
        scale = values[0].scale
        integer = sum(item.integer for item in values)
        exact = sum((item.exact for item in values), Fraction(0))
        error_bound = sum((item.error_bound for item in values), Fraction(0))
        result = FixedValue(
            subject=subject,
            kind="addition_result",
            exact=exact,
            scale=scale,
            integer=integer,
            rounding="integer_addition",
            discarded_remainder=Fraction(0),
            error_bound=error_bound,
            conversion_id=f"add-{len(self.operations) + 1:04d}",
            source_batches=tuple(seq for item in values for seq in item.source_batches),
        )
        self.cumulative_add_error_bound += error_bound
        self.operations.append(
            {
                "operation_id": result.conversion_id,
                "op": "add",
                "subject": subject,
                "inputs": [item.conversion_id for item in values],
                "integer_value": integer,
                "error_bound": frac_str(error_bound),
                "cumulative_add_error_bound": frac_str(self.cumulative_add_error_bound),
            }
        )
        return result

    def multiply(self, left: FixedValue, right: FixedValue, *, subject: str) -> FixedValue:
        if left.scale != right.scale:
            raise ValueError("fixed-point multiplication requires identical scales")
        scale = left.scale
        represented_left = Fraction(left.integer, scale)
        represented_right = Fraction(right.integer, scale)
        propagation_bound = (
            abs(represented_left) * right.error_bound
            + abs(represented_right) * left.error_bound
            + left.error_bound * right.error_bound
        )
        raw_scaled = Fraction(left.integer * right.integer, scale)
        floor_int = raw_scaled.numerator // raw_scaled.denominator
        fractional = raw_scaled - floor_int
        if fractional == 0:
            integer = floor_int
            direction = "exact"
            rounding_bound = Fraction(0)
        elif self.default_rounding == "ceil":
            integer = floor_int + 1
            direction = "ceil_up"
            rounding_bound = Fraction(1, scale)
        else:
            integer = floor_int
            direction = "floor_down"
            rounding_bound = Fraction(1, scale)
        error_bound = propagation_bound + rounding_bound
        exact = left.exact * right.exact
        result = FixedValue(
            subject=subject,
            kind="multiplication_result",
            exact=exact,
            scale=scale,
            integer=integer,
            rounding=direction,
            discarded_remainder=raw_scaled - floor_int if direction == "floor_down" else Fraction(1) - fractional,
            error_bound=error_bound,
            conversion_id=f"mul-{len(self.operations) + 1:04d}",
            source_batches=tuple(sorted(set(left.source_batches + right.source_batches))),
        )
        self.cumulative_mul_error_bound += error_bound
        self.operations.append(
            {
                "operation_id": result.conversion_id,
                "op": "mul",
                "subject": subject,
                "inputs": [left.conversion_id, right.conversion_id],
                "integer_value": integer,
                "rounding": direction,
                "propagation_error_bound": frac_str(propagation_bound),
                "rounding_error_bound": frac_str(rounding_bound),
                "error_bound": frac_str(error_bound),
                "cumulative_mul_error_bound": frac_str(self.cumulative_mul_error_bound),
            }
        )
        return result

    def source_chain(self, fixed_values: Iterable[FixedValue]) -> dict[str, Any]:
        values = tuple(fixed_values)
        top_ids = {value.conversion_id for value in values}
        operation_ids: set[str] = set()
        op_by_id = {op["operation_id"]: op for op in self.operations}
        conversion_ids = set(top_ids)
        pending = [op_by_id[value_id] for value_id in top_ids if value_id in op_by_id]
        pending.extend(
            op
            for op in self.operations
            if any(input_id in top_ids for input_id in op.get("inputs", []))
        )
        while pending:
            op = pending.pop()
            operation_id = op["operation_id"]
            if operation_id in operation_ids:
                continue
            operation_ids.add(operation_id)
            for input_id in op["inputs"]:
                parent = op_by_id.get(input_id)
                if parent is not None:
                    pending.append(parent)
                else:
                    conversion_ids.add(input_id)
        batches = sorted({seq for value in values for seq in value.source_batches})
        bound = sum((value.error_bound for value in values), Fraction(0))
        return {
            "participating_batches": batches,
            "conversion_ids": sorted(conversion_ids),
            "operation_ids": sorted(operation_ids),
            "error_upper_bound": frac_str(bound),
            "scale": self.scale,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "default_rounding": self.default_rounding,
            "conversions": [item.to_dict() for item in self.conversions],
            "operations": self.operations,
            "cumulative_add_error_bound": frac_str(self.cumulative_add_error_bound),
            "cumulative_mul_error_bound": frac_str(self.cumulative_mul_error_bound),
        }
