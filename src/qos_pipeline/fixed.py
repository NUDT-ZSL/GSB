"""Deterministic fixed-point arithmetic for rates, buckets, and quotas."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


FIXED_SCALE = 1_000
TOKEN_DENOMINATOR = 8_000


def bytes_to_units(value: int) -> int:
    return value * FIXED_SCALE


def units_to_bytes_exact(units: int) -> Fraction:
    return Fraction(units, FIXED_SCALE)


def units_to_bytes_text(units: int) -> str:
    sign = "-" if units < 0 else ""
    units = abs(units)
    whole, fraction = divmod(units, FIXED_SCALE)
    return f"{sign}{whole}.{fraction:03d}"


def fraction_text(value: Fraction, digits: int = 6) -> str:
    scale = 10**digits
    negative = value < 0
    scaled = abs(value.numerator) * scale
    quotient, remainder = divmod(scaled, value.denominator)
    if remainder * 2 >= value.denominator:
        quotient += 1
    sign = "-" if negative else ""
    whole, fraction = divmod(quotient, scale)
    return f"{sign}{whole}.{fraction:0{digits}d}"


@dataclass(frozen=True)
class RateConversion:
    class_name: str
    field: str
    raw_value: int
    raw_float: str
    scale: int
    denominator: int
    numerator: int
    increment_units: int
    rate_remainder: int
    rounding: str
    exact_increment: Fraction
    error_upper_units: Fraction
    step_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "class": self.class_name,
            "field": self.field,
            "raw_value": self.raw_value,
            "raw_float": self.raw_float,
            "scale": self.scale,
            "unit": "fixed_token_units_per_byte",
            "denominator": self.denominator,
            "numerator": self.numerator,
            "formula": f"raw_value * step_ms({self.step_ms}) * scale / {self.denominator}",
            "increment_units": self.increment_units,
            "rate_remainder": self.rate_remainder,
            "rounding": self.rounding,
            "exact_increment": fraction_text(self.exact_increment, 12),
            "error_upper_units": fraction_text(self.error_upper_units, 12),
        }


def convert_rate(rate_bps: int, step_ms: int, class_name: str, field: str) -> RateConversion:
    numerator = rate_bps * step_ms * FIXED_SCALE
    increment, remainder = divmod(numerator, TOKEN_DENOMINATOR)
    exact = Fraction(numerator, TOKEN_DENOMINATOR)
    return RateConversion(
        class_name=class_name,
        field=field,
        raw_value=rate_bps,
        raw_float=f"{float(rate_bps):.1f}",
        scale=FIXED_SCALE,
        denominator=TOKEN_DENOMINATOR,
        numerator=numerator,
        increment_units=increment,
        rate_remainder=remainder,
        rounding="floor",
        exact_increment=exact,
        error_upper_units=Fraction(remainder, TOKEN_DENOMINATOR),
        step_ms=step_ms,
    )


def convert_bucket_depth(depth_bytes: int, class_name: str, field: str) -> dict[str, object]:
    return {
        "class": class_name,
        "field": field,
        "raw_value": depth_bytes,
        "raw_float": f"{float(depth_bytes):.1f}",
        "scale": FIXED_SCALE,
        "unit": "fixed_token_units_per_byte",
        "formula": f"raw_value * scale({FIXED_SCALE})",
        "increment_units": bytes_to_units(depth_bytes),
        "rate_remainder": 0,
        "rounding": "exact",
        "exact_increment": str(FIXED_SCALE * depth_bytes),
        "error_upper_units": "0.000000000000",
    }


def add_refill(
    tokens_units: int,
    capacity_units: int,
    increment_units: int,
    rate_remainder: int,
    saved_remainder: int,
) -> tuple[int, int, int, int]:
    """Add one fixed-point refill.

    Returns (tokens, saved_remainder, added_units, overflow_units).
    The saved remainder contains the subunits that could not become a whole
    fixed-point token. It is carried separately for every bucket.
    """

    carried = saved_remainder + rate_remainder
    extra_units, new_remainder = divmod(carried, TOKEN_DENOMINATOR)
    requested = increment_units + extra_units
    room = max(0, capacity_units - tokens_units)
    added = min(room, requested)
    overflow = requested - added
    return tokens_units + added, new_remainder, added, overflow


def consume_tokens(
    committed_units: int, peak_units: int, requested_units: int
) -> tuple[int, int] | None:
    if requested_units < 0:
        raise ValueError("requested token units cannot be negative")
    if committed_units + peak_units < requested_units:
        return None
    from_committed = min(committed_units, requested_units)
    from_peak = requested_units - from_committed
    return committed_units - from_committed, peak_units - from_peak


def proportional_allocate(total: int, weights: list[int]) -> list[int]:
    """Allocate integer units proportionally with stable largest-remainder ties."""

    if total < 0:
        raise ValueError("total cannot be negative")
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("weights must be a non-empty list of positive integers")
    weight_sum = sum(weights)
    raw = [total * weight for weight in weights]
    values = [value // weight_sum for value in raw]
    remainders = [value % weight_sum for value in raw]
    remaining = total - sum(values)
    order = sorted(range(len(weights)), key=lambda index: (-remainders[index], index))
    for index in order[:remaining]:
        values[index] += 1
    return values


def conversion_ledger(sample: object) -> list[dict[str, object]]:
    """Build a stable conversion ledger without relying on object mutation."""

    rows: list[dict[str, object]] = []
    for class_config in sample.classes:  # type: ignore[attr-defined]
        rows.append(
            convert_rate(
                class_config.cir_bps, sample.step_ms, class_config.name, "committed_rate"  # type: ignore[attr-defined]
            ).as_dict()
        )
        rows.append(
            convert_rate(
                class_config.pir_bps, sample.step_ms, class_config.name, "peak_rate"  # type: ignore[attr-defined]
            ).as_dict()
        )
        if class_config.guarantee_bps is not None:
            rows.append(
                convert_rate(
                    class_config.guarantee_bps,
                    sample.step_ms,
                    class_config.name,
                    "guaranteed_bandwidth",
                ).as_dict()
            )
        rows.append(
            convert_bucket_depth(
                class_config.committed_burst_bytes, class_config.name, "committed_burst"
            )
        )
        rows.append(
            convert_bucket_depth(
                class_config.peak_burst_bytes, class_config.name, "peak_burst"
            )
        )
        rows.append(
            convert_bucket_depth(
                class_config.queue_depth_bytes, class_config.name, "queue_depth"
            )
        )
    return sorted(rows, key=lambda row: (str(row["class"]), str(row["field"])))

