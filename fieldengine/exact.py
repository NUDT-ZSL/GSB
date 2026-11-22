from __future__ import annotations

import math
import struct
from fractions import Fraction
from typing import Iterable

import numpy as np


_LIMB_BITS = 13
_LIMB_BASE = 1 << _LIMB_BITS
_LIMB_MASK = _LIMB_BASE - 1
_MANTISSA_LIMBS = 5


def float_bits(value: float) -> str:
    if math.isnan(value):
        return "7ff8000000000000"
    return struct.pack(">d", float(value)).hex()


def json_number(value: float) -> float | str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return float(value)


def bins_from_pairs(pairs: Iterable[Iterable[int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for exponent, mantissa in pairs:
        out[int(exponent)] = out.get(int(exponent), 0) + int(mantissa)
    return out


def bins_to_pairs(bins: dict[int, int]) -> list[list[int]]:
    return [[int(e), int(m)] for e, m in sorted(bins.items()) if m]


def add_bins(target: dict[int, int], source: dict[int, int]) -> None:
    for exponent, mantissa in source.items():
        target[exponent] = target.get(exponent, 0) + mantissa


def bins_to_fraction(bins: dict[int, int]) -> Fraction:
    useful = {e: m for e, m in bins.items() if m}
    if not useful:
        return Fraction(0, 1)
    min_exp = min(useful)
    numerator = 0
    for exponent, mantissa in useful.items():
        numerator += mantissa << (exponent - min_exp)
    if min_exp >= 0:
        return Fraction(numerator << min_exp, 1)
    return Fraction(numerator, 1 << (-min_exp))


def fraction_to_float(value: Fraction) -> float:
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _bin_reduce(
    group_ids: np.ndarray,
    exponents: np.ndarray,
    magnitudes: np.ndarray,
    signs: np.ndarray | None,
    n_groups: int,
) -> list[dict[int, int]]:
    out: list[dict[int, int]] = [dict() for _ in range(n_groups)]
    if group_ids.size == 0:
        return out
    order = np.lexsort((exponents, group_ids))
    groups = group_ids[order]
    exps = exponents[order]
    mags = magnitudes[order]
    if signs is None:
        sign_sorted = np.zeros(groups.shape, dtype=np.bool_)
    else:
        sign_sorted = signs[order]
    starts = np.flatnonzero(
        np.r_[True, (groups[1:] != groups[:-1]) | (exps[1:] != exps[:-1])]
    )
    ends = np.r_[starts[1:], groups.size]
    for start, end in zip(starts.tolist(), ends.tolist()):
        total = 0
        if signs is None:
            for limb in range(_MANTISSA_LIMBS):
                limb_values = (mags[start:end] >> (limb * _LIMB_BITS)) & _LIMB_MASK
                total += int(limb_values.sum()) << (limb * _LIMB_BITS)
        else:
            pos = ~sign_sorted[start:end]
            neg = sign_sorted[start:end]
            for limb in range(_MANTISSA_LIMBS):
                limb_values = (mags[start:end] >> (limb * _LIMB_BITS)) & _LIMB_MASK
                pos_sum = int(limb_values[pos].sum())
                neg_sum = int(limb_values[neg].sum())
                total += (pos_sum - neg_sum) << (limb * _LIMB_BITS)
        out[int(groups[start])][int(exps[start])] = total
    return out


def _square_bin_reduce(
    group_ids: np.ndarray,
    exponents: np.ndarray,
    magnitudes: np.ndarray,
    n_groups: int,
) -> list[dict[int, int]]:
    out: list[dict[int, int]] = [dict() for _ in range(n_groups)]
    if group_ids.size == 0:
        return out
    order = np.lexsort((exponents, group_ids))
    groups = group_ids[order]
    exps = exponents[order]
    mags = magnitudes[order]
    starts = np.flatnonzero(
        np.r_[True, (groups[1:] != groups[:-1]) | (exps[1:] != exps[:-1])]
    )
    ends = np.r_[starts[1:], groups.size]
    limbs = [
        (mags >> (limb * _LIMB_BITS)) & _LIMB_MASK for limb in range(_MANTISSA_LIMBS)
    ]
    products: dict[int, np.ndarray] = {}
    reduced: dict[int, np.ndarray] = {}
    for left in range(_MANTISSA_LIMBS):
        for right in range(left, _MANTISSA_LIMBS):
            shift = left + right
            product = limbs[left] * limbs[right]
            if left != right:
                product = product << 1
            products[shift] = product
            reduced[shift] = np.add.reduceat(product, starts)
    for index, (start, _end) in enumerate(zip(starts.tolist(), ends.tolist())):
        total = 0
        for shift, values in reduced.items():
            coefficient = int(values[index])
            if coefficient:
                total += coefficient << (shift * _LIMB_BITS)
        out[int(groups[start])][int(exps[start])] = total
    return out


def exact_reduce(
    values: np.ndarray,
    group_ids: np.ndarray,
    n_groups: int,
) -> dict[str, object]:
    values = np.asarray(values, dtype=np.float64)
    group_ids = np.asarray(group_ids, dtype=np.int64)
    counts = np.bincount(group_ids, minlength=n_groups).astype(np.int64)
    finite = np.isfinite(values)
    nan_mask = np.isnan(values)
    posinf_mask = np.isposinf(values)
    neginf_mask = np.isneginf(values)
    anomalies = {
        "nan": np.bincount(group_ids[nan_mask], minlength=n_groups).astype(np.int64).tolist(),
        "posinf": np.bincount(group_ids[posinf_mask], minlength=n_groups).astype(np.int64).tolist(),
        "neginf": np.bincount(group_ids[neginf_mask], minlength=n_groups).astype(np.int64).tolist(),
    }
    finite_ids = group_ids[finite]
    finite_values = values[finite]
    nonzero = finite_values != 0.0
    finite_ids = finite_ids[nonzero]
    finite_values = finite_values[nonzero]
    if finite_values.size:
        magnitudes, binary_exp = np.frexp(np.abs(finite_values))
        mantissa = np.ldexp(magnitudes, 53).astype(np.int64)
        exponent = binary_exp.astype(np.int64) - 53
        signs = np.signbit(finite_values)
        sum_bins = _bin_reduce(finite_ids, exponent, mantissa, signs, n_groups)
        square_bins = _square_bin_reduce(finite_ids, exponent << 1, mantissa, n_groups)
    else:
        sum_bins = [dict() for _ in range(n_groups)]
        square_bins = [dict() for _ in range(n_groups)]
    max_abs = np.zeros(n_groups, dtype=np.float64)
    if finite_values.size:
        np.maximum.at(max_abs, finite_ids, np.abs(finite_values))
    return {
        "counts": counts.tolist(),
        "sum_bins": sum_bins,
        "square_bins": square_bins,
        "anomalies": anomalies,
        "max_abs": max_abs.tolist(),
    }


def sum_float_from_bins(bins: dict[int, int], anomaly: dict[str, int]) -> float:
    if anomaly.get("nan", 0) > 0:
        return math.nan
    if anomaly.get("posinf", 0) > 0 and anomaly.get("neginf", 0) > 0:
        return math.nan
    if anomaly.get("posinf", 0) > 0:
        return math.inf
    if anomaly.get("neginf", 0) > 0:
        return -math.inf
    return fraction_to_float(bins_to_fraction(bins))


def mean_float_from_bins(
    bins: dict[int, int], anomaly: dict[str, int], count: int
) -> float:
    total = sum_float_from_bins(bins, anomaly)
    return total / count if count else math.nan


def variance_float_from_bins(
    sum_bins: dict[int, int],
    square_bins: dict[int, int],
    anomaly: dict[str, int],
    count: int,
) -> float:
    if count == 0:
        return math.nan
    if any(anomaly.get(name, 0) > 0 for name in ("nan", "posinf", "neginf")):
        return math.nan
    total = bins_to_fraction(sum_bins)
    square_total = bins_to_fraction(square_bins)
    variance = square_total / count - (total / count) ** 2
    if variance < 0:
        variance = Fraction(0, 1)
    return fraction_to_float(variance)


def fixed_variance_fraction(
    fixed_sum: int, fixed_square_sum: int, count: int, scale: int
) -> Fraction:
    if count <= 0:
        return Fraction(0, 1)
    numerator = count * fixed_square_sum - fixed_sum * fixed_sum
    if numerator < 0:
        numerator = 0
    factor = 10**scale
    return Fraction(numerator, count * count * factor * factor)
