from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Dict


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def stable_hash_int(material: str, modulus: int) -> int:
    if modulus <= 0:
        return 0
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:16], "big", signed=False) % modulus


def round_decimal_to_ms(value: Any, rounding: str = "ceil") -> Dict[str, Any]:
    """Convert seconds to integer milliseconds with explicit rounding."""

    decimal_value = Decimal(str(value))
    scaled = decimal_value * Decimal(1000)
    if rounding in {"ceil", "ceiling", "toward_positive_infinity"}:
        rounded = scaled.to_integral_value(rounding="ROUND_CEILING")
        direction = "ceil_to_positive_infinity"
    elif rounding == "floor":
        rounded = scaled.to_integral_value(rounding="ROUND_FLOOR")
        direction = "floor_to_negative_infinity"
    elif rounding in {"nearest", "half_up"}:
        rounded = scaled.to_integral_value(rounding="ROUND_HALF_UP")
        direction = "nearest_half_up"
    else:
        raise ValueError(f"unsupported rounding mode: {rounding}")
    return {
        "from_unit": "seconds",
        "to_unit": "milliseconds",
        "scale": 1000,
        "rounding_direction": direction,
        "input_seconds": str(decimal_value),
        "scaled_milliseconds": str(scaled),
        "rounded_milliseconds": int(rounded),
    }


_CAMEL_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    name = _CAMEL_RE_1.sub(r"\1_\2", name)
    name = _CAMEL_RE_2.sub(r"\1_\2", name)
    return name.replace("-", "_").lower()
