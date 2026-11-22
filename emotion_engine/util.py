"""Small deterministic utilities shared by all modules."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Sequence


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def sanitize_float(value: float, subnormal_guard: float) -> float:
    """Replace non-finite values and true subnormals with a stable zero."""

    if not math.isfinite(value) or abs(value) < subnormal_guard:
        return 0.0
    return 0.0 if value == 0.0 else value


def stable_mean(values: Iterable[float], subnormal_guard: float) -> float:
    ordered = [sanitize_float(float(value), subnormal_guard) for value in values]
    if not ordered:
        return 0.0
    return sanitize_float(math.fsum(ordered) / len(ordered), subnormal_guard)


def rounded(value: float, decimals: int, subnormal_guard: float) -> float:
    return round(sanitize_float(float(value), subnormal_guard), decimals)


def merge_intervals(intervals: Sequence[Sequence[int]]) -> list[list[int]]:
    """Merge inclusive integer intervals and discard invalid ones."""

    normalized = sorted(
        [int(start), int(end)]
        for start, end in intervals
        if int(start) <= int(end)
    )
    merged: list[list[int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def complement_intervals(intervals: Sequence[Sequence[int]], total: int) -> list[list[int]]:
    if total <= 0:
        return []
    merged = merge_intervals(intervals)
    result: list[list[int]] = []
    cursor = 0
    for start, end in merged:
        if start >= total:
            break
        if start > cursor:
            result.append([cursor, min(total - 1, start - 1)])
        cursor = max(cursor, min(total, end + 1))
    if cursor < total:
        result.append([cursor, total - 1])
    return result


def intervals_cover_all(recomputed: Sequence[Sequence[int]], reused: Sequence[Sequence[int]], total: int) -> bool:
    merged = merge_intervals(list(recomputed) + list(reused))
    return merged == [[0, total - 1]] if total else merged == []


def intervals_overlap(recomputed: Sequence[Sequence[int]], reused: Sequence[Sequence[int]]) -> bool:
    left = merge_intervals(recomputed)
    right = merge_intervals(reused)
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i][1] < right[j][0]:
            i += 1
        elif right[j][1] < left[i][0]:
            j += 1
        else:
            return True
    return False
