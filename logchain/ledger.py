from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable


def as_fraction(value: int | str | Fraction) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"unsupported fixed-point raw value: {type(value)!r}")


def fraction_key(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def convert_fixed(
    *,
    conversion_id: str,
    generation: int,
    kind: str,
    raw_value: int | str | Fraction,
    scale: int,
    rounding: str = "floor",
    table_name: str | None = None,
    row_key: str | None = None,
) -> dict[str, Any]:
    """Convert a raw value to a deterministic fixed-point integer.

    The returned remainder and error bound are exact rational numbers.  JSON
    serialization uses numerator/denominator pairs, so status output is stable
    across processes and Python versions.
    """

    if not isinstance(scale, int) or scale <= 0:
        raise ValueError("scale must be a positive integer")
    raw = as_fraction(raw_value)
    scaled_raw = raw * scale

    if rounding == "floor":
        scaled = scaled_raw.numerator // scaled_raw.denominator
        direction = "exact" if scaled_raw == scaled else "floor_down"
        rounded = Fraction(scaled, scale)
        discarded = raw - rounded
        error_bound = Fraction(1, scale)
    elif rounding == "ceil":
        scaled = -((-scaled_raw.numerator) // scaled_raw.denominator)
        direction = "exact" if scaled_raw == scaled else "ceil_up"
        rounded = Fraction(scaled, scale)
        discarded = rounded - raw
        error_bound = Fraction(1, scale)
    elif rounding == "nearest":
        scaled = (2 * scaled_raw.numerator + scaled_raw.denominator) // (
            2 * scaled_raw.denominator
        )
        if scaled_raw == scaled:
            direction = "exact"
        elif Fraction(scaled, 1) > scaled_raw:
            direction = "nearest_up"
        else:
            direction = "nearest_down"
        rounded = Fraction(scaled, scale)
        discarded = abs(raw - rounded)
        error_bound = Fraction(1, 2 * scale)
    else:
        raise ValueError(f"unsupported rounding mode: {rounding}")

    exact_error = abs(raw - rounded)
    return {
        "conversion_id": conversion_id,
        "generation": generation,
        "kind": kind,
        "table_name": table_name,
        "row_key": row_key,
        "raw": fraction_key(raw),
        "raw_text": fraction_text(raw),
        "scale": scale,
        "rounding_mode": rounding,
        "rounding_direction": direction,
        "scaled_value": scaled,
        "unit": "ms_at_scale",
        "discarded_remainder": fraction_key(discarded),
        "discarded_remainder_text": fraction_text(discarded),
        "exact_abs_error": fraction_key(exact_error),
        "exact_abs_error_text": fraction_text(exact_error),
        "error_upper_bound": fraction_key(error_bound),
        "error_upper_bound_text": fraction_text(error_bound),
    }


def _bound_from_conversions(conversions: Iterable[dict[str, Any]]) -> Fraction:
    total = Fraction(0, 1)
    for conversion in conversions:
        bound = conversion["error_upper_bound"]
        total += Fraction(bound["numerator"], bound["denominator"])
    return total


def build_generation_ledger(
    *,
    generation: int,
    actual_commit_ms: int,
    theoretical_lower_bound_ms: int | str | Fraction,
    scale: int = 100,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual = convert_fixed(
        conversion_id=f"conv-g{generation}-actual",
        generation=generation,
        kind="actual_commit_ms",
        raw_value=actual_commit_ms,
        scale=scale,
        rounding="nearest",
    )
    lower = convert_fixed(
        conversion_id=f"conv-g{generation}-lower-bound",
        generation=generation,
        kind="theoretical_commit_lower_bound_ms",
        raw_value=theoretical_lower_bound_ms,
        scale=scale,
        rounding="floor",
    )
    current_bound = _bound_from_conversions([actual, lower])
    parent_bound = Fraction(0, 1)
    dependency_chain: list[str] = []
    operations: list[dict[str, Any]] = []
    if parent is not None:
        parent_bound = Fraction(
            parent["cumulative_error_bound"]["numerator"],
            parent["cumulative_error_bound"]["denominator"],
        )
        dependency_chain = list(parent["dependency_chain"])
        operations.extend(parent["operations"])
        dependency_chain.append(f"ledger-g{parent['generation']}->g{generation}")
        operations.append(
            {
                "operation_id": f"add-g{parent['generation']}-g{generation}",
                "op": "add_error_bounds",
                "operands": [
                    f"cumulative:g{parent['generation']}",
                    f"conv-g{generation}-actual",
                    f"conv-g{generation}-lower-bound",
                ],
                "error_upper_bound": fraction_key(parent_bound + current_bound),
                "error_upper_bound_text": fraction_text(parent_bound + current_bound),
            }
        )
    else:
        operations.append(
            {
                "operation_id": f"add-g{generation}-base",
                "op": "add_error_bounds",
                "operands": [
                    f"conv-g{generation}-actual",
                    f"conv-g{generation}-lower-bound",
                ],
                "error_upper_bound": fraction_key(current_bound),
                "error_upper_bound_text": fraction_text(current_bound),
            }
        )

    cumulative = parent_bound + current_bound
    dependency_chain.extend(
        [f"conv-g{generation}-actual", f"conv-g{generation}-lower-bound"]
    )
    return {
        "generation": generation,
        "scale": scale,
        "actual_commit_ms": actual_commit_ms,
        "theoretical_lower_bound_ms": fraction_key(as_fraction(theoretical_lower_bound_ms)),
        "actual_conversion": actual,
        "lower_bound_conversion": lower,
        "cumulative_error_bound": fraction_key(cumulative),
        "cumulative_error_bound_text": fraction_text(cumulative),
        "dependency_chain": dependency_chain,
        "operations": operations,
    }


def compare_adjacent(
    older: dict[str, Any],
    newer: dict[str, Any],
    *,
    tolerance_ms: int | str | Fraction,
    decision_depends_on_gap: bool = True,
) -> dict[str, Any]:
    gap = Fraction(newer["actual_commit_ms"] - older["actual_commit_ms"], 1)
    tolerance = as_fraction(tolerance_ms)
    older_bound = Fraction(
        older["cumulative_error_bound"]["numerator"],
        older["cumulative_error_bound"]["denominator"],
    )
    newer_bound = Fraction(
        newer["cumulative_error_bound"]["numerator"],
        newer["cumulative_error_bound"]["denominator"],
    )
    comparison_bound = older_bound + newer_bound
    sensitive = decision_depends_on_gap and abs(gap) < tolerance
    source_chain = [
        *newer["dependency_chain"],
        f"compare-g{older['generation']}-g{newer['generation']}",
        f"direct:{older['actual_conversion']['conversion_id']}",
        f"direct:{newer['actual_conversion']['conversion_id']}",
    ]
    return {
        "adjacent_generations": [older["generation"], newer["generation"]],
        "commit_gap_ms": fraction_key(gap),
        "commit_gap_ms_text": fraction_text(gap),
        "tolerance_ms": fraction_key(tolerance),
        "tolerance_ms_text": fraction_text(tolerance),
        "decision_depends_on_gap": decision_depends_on_gap,
        "comparison_error_upper_bound_ms": fraction_key(comparison_bound),
        "comparison_error_upper_bound_ms_text": fraction_text(comparison_bound),
        "precision_sensitive": sensitive,
        "error_source_chain": source_chain,
        "recovery_rule": (
            "pin canonical generation/transaction order and replay the complete suffix"
            if sensitive
            else "normal watermark comparison is unambiguous"
        ),
    }
