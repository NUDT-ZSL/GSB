from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any


def decimal_to_fraction(value: Decimal | str | int) -> Fraction:
    return Fraction(Decimal(str(value)))


def fraction_decimal(value: Fraction, precision: int = 80) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision
        return Decimal(value.numerator) / Decimal(value.denominator)


def fraction_json(value: Fraction) -> dict[str, int | str]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "decimal": format(fraction_decimal(value), "f"),
    }


def round_half_up(value: Fraction) -> tuple[int, Fraction, Fraction, str]:
    """Round a non-negative fraction to nearest integer, halves upward.

    Returns quotient, signed residual exact - quotient, pre-round fractional
    margin, and a stable rounding direction.
    """
    if value < 0:
        raise ValueError("capacity fixed-point inputs are non-negative")
    floor_q, remainder = divmod(value.numerator, value.denominator)
    fractional = Fraction(remainder, value.denominator)
    if remainder == 0:
        return floor_q, Fraction(0, 1), fractional, "exact"
    if remainder * 2 >= value.denominator:
        quotient = floor_q + 1
        direction = "round_up_half_up" if remainder * 2 == value.denominator else "round_up"
    else:
        quotient = floor_q
        direction = "round_down"
    residual = value - quotient
    return quotient, residual, fractional, direction


@dataclass
class ConversionChain:
    metric: str
    scale: int
    operations: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    register_ids: list[int] = field(default_factory=list)

    def log(
        self,
        op: str,
        *,
        output: Any = None,
        error_bound: Fraction = Fraction(0),
        cumulative_bound: Fraction | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        seq = self.next_seq
        self.next_seq += 1
        register_id = len(self.register_ids) + 1
        self.register_ids.append(register_id)
        details.setdefault("rounding_direction", "exact_no_rounding")
        details.setdefault("discarded_margin", Fraction(0, 1))
        record: dict[str, Any] = {
            "registration_seq": seq,
            "register_id": register_id,
            "op": op,
            "error_bound": format(fraction_decimal(error_bound), "f"),
            "cumulative_error_bound": format(
                fraction_decimal(cumulative_bound if cumulative_bound is not None else error_bound),
                "f",
            ),
        }
        if output is not None:
            record["output"] = output
        for key, value in details.items():
            if isinstance(value, Fraction):
                record[key] = fraction_json(value)
            else:
                record[key] = value
        self.operations.append(record)
        return record


@dataclass
class FixedMetricResult:
    fixed_value: int
    raw_value: Fraction
    carry_in: Fraction
    carry_out: Fraction
    discarded: Fraction
    error_bound: Fraction
    chain: ConversionChain
    operations: list[dict[str, Any]]


def convert_with_carry(
    *,
    metric: str,
    scale: int,
    raw_value: Fraction,
    carry_in: Fraction,
    formula_ops: list[dict[str, Any]] | None = None,
) -> FixedMetricResult:
    chain = ConversionChain(metric=metric, scale=scale)
    if formula_ops:
        for operation in formula_ops:
            seq = chain.next_seq
            chain.next_seq += 1
            register_id = len(chain.register_ids) + 1
            chain.register_ids.append(register_id)
            record = dict(operation)
            record["registration_seq"] = seq
            record["register_id"] = register_id
            record.setdefault("rounding_direction", "exact_no_rounding")
            record.setdefault("discarded_margin", Fraction(0, 1))
            record.setdefault("error_bound", Fraction(0, 1))
            record.setdefault("cumulative_error_bound", Fraction(0, 1))
            chain.operations.append(record)

    cumulative = Fraction(0)
    chain.log(
        "scale_multiply",
        left=fraction_json(raw_value),
        right=scale,
        output=fraction_json(raw_value * scale),
        error_bound=Fraction(0),
        cumulative_bound=cumulative,
    )
    scaled = raw_value * scale

    carry_bound = Fraction(1, 2 * scale)
    cumulative = carry_bound
    chain.log(
        "add_carry",
        left=fraction_json(scaled),
        carry_in_fixed_units=fraction_json(carry_in),
        output=fraction_json(scaled + carry_in),
        incoming_error_bound=format(fraction_decimal(carry_bound), "f"),
        error_bound=carry_bound,
        cumulative_bound=cumulative,
    )
    with_carry = scaled + carry_in

    fixed_value, carry_out, discarded, direction = round_half_up(with_carry)
    local_bound = Fraction(1, 2 * scale)
    total_bound = Fraction(1, scale)
    chain.log(
        "fixed_divide_round",
        numerator=fraction_json(with_carry),
        divisor=1,
        quotient=fixed_value,
        signed_residual_fixed_units=carry_out,
        discarded_margin=discarded,
        discarded_pre_round_margin_fixed_units=discarded,
        rounding_direction=direction,
        rounding_error_bound=format(fraction_decimal(local_bound), "f"),
        error_bound=total_bound,
        cumulative_bound=total_bound,
    )

    return FixedMetricResult(
        fixed_value=fixed_value,
        raw_value=raw_value,
        carry_in=carry_in,
        carry_out=carry_out,
        discarded=discarded,
        error_bound=total_bound,
        chain=chain,
        operations=chain.operations,
    )


def high_scale_value(raw_value: Fraction, scale: int) -> int:
    quotient, _, _, _ = round_half_up(raw_value * scale)
    return quotient
