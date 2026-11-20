"""Canonical serialization and exact numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_digest(value: str) -> str:
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def decimal_of(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TypeError(f"not a numeric value: {value!r}")
    return Decimal(str(value))


def decimal_text(value: Decimal | int | float | str) -> str:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


ROUNDING_MODES = {
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "nearest": ROUND_HALF_UP,
    "toward_zero": ROUND_DOWN,
}


def scaled_milliseconds(value: Any, scale_ms: int, rounding: str) -> int:
    mode = ROUNDING_MODES.get(rounding)
    if mode is None:
        raise ValueError(f"unsupported rounding mode: {rounding}")
    scaled = decimal_of(value) * Decimal(scale_ms)
    return int(scaled.quantize(Decimal("1"), rounding=mode))
