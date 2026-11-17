"""Integer fixed-point primitives used by aggregation, embedding, and similarity."""

from __future__ import annotations

import math

from .canonical import clamp_int, round_half_away_ratio

INT64_MAX = (1 << 63) - 1
SQRT_FRACTION_BITS = 16


def q_round_div(numerator: int, denominator: int) -> int:
    return round_half_away_ratio(int(numerator), int(denominator))


def fixed_to_float(value: int, bits: int) -> float:
    return int(value) / float(1 << bits)


def fixed_sqrt(norm_square: int, fraction_bits: int = SQRT_FRACTION_BITS) -> int:
    """Return round(sqrt(norm_square) * 2**fraction_bits) using integer sqrt."""
    if norm_square < 0:
        raise ValueError("norm square must be non-negative")
    if norm_square == 0:
        return 0
    return math.isqrt(int(norm_square) << (2 * fraction_bits))


def fixed_square_norm(values: list[int] | tuple[int, ...]) -> int:
    total = 0
    for value in values:
        total += int(value) * int(value)
    return total


def normalize_fixed_value(value_q: int, source_bits: int, scale: int, target_bits: int) -> int:
    """Compute round(value_q / 2**source_bits / scale * 2**target_bits)."""
    if source_bits < 0 or target_bits < 0 or scale <= 0:
        raise ValueError("invalid normalization parameters")
    return q_round_div(int(value_q) * (1 << target_bits), int(scale) * (1 << source_bits))


def int64_overflow_risk(max_absolute_accumulator: int) -> bool:
    return int(max_absolute_accumulator) > INT64_MAX


def quantized_code(value_q: int, half_levels: int, clip_q: int) -> int:
    if clip_q <= 0:
        raise ValueError("clip must be positive")
    if half_levels <= 0:
        return 0
    return clamp_int(q_round_div(int(value_q) * half_levels, clip_q), -half_levels, half_levels)


def unitize_codes(codes: list[int] | tuple[int, ...], bits: int) -> list[int]:
    """Unitize integer level codes using an integer square root and Q rounding."""
    norm_square = fixed_square_norm(codes)
    if norm_square == 0:
        return [0 for _ in codes]
    root = fixed_sqrt(norm_square, SQRT_FRACTION_BITS)
    if root == 0:
        return [0 for _ in codes]
    output: list[int] = []
    for code in codes:
        numerator = int(code) << (bits + SQRT_FRACTION_BITS)
        output.append(q_round_div(numerator, root))
    return output


def cosine_fixed(
    left: list[int] | tuple[int, ...],
    right: list[int] | tuple[int, ...],
    output_scale: int,
) -> tuple[int, dict[str, int]]:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    dot = 0
    left_square = 0
    right_square = 0
    for left_item, right_item in zip(left, right):
        left_int = int(left_item)
        right_int = int(right_item)
        dot += left_int * right_int
        left_square += left_int * left_int
        right_square += right_int * right_int
    left_root = fixed_sqrt(left_square, SQRT_FRACTION_BITS)
    right_root = fixed_sqrt(right_square, SQRT_FRACTION_BITS)
    diagnostics = {
        "dot": dot,
        "left_norm_square": left_square,
        "right_norm_square": right_square,
        "left_norm_q16": left_root,
        "right_norm_q16": right_root,
        "max_absolute_accumulator": abs(dot),
    }
    if left_root == 0 or right_root == 0:
        return 0, diagnostics
    numerator = dot * output_scale * (1 << (2 * SQRT_FRACTION_BITS))
    denominator = left_root * right_root
    similarity = q_round_div(numerator, denominator)
    return clamp_int(similarity, -output_scale, output_scale), diagnostics

