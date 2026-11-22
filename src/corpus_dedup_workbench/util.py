from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def decimal_text(value: Decimal | str | float) -> str:
    if isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def rounded_float(value: float | None, precision: int = 12) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        return value
    return round(float(value), precision)


def public_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value

