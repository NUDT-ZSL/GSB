from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def normalize_numbers(value: Any) -> Any:
    """Normalize floats so canonical hashes are stable across JSON round-trips."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/Inf are not allowed in canonical content")
        rounded = round(value, 12)
        if rounded == 0:
            return 0.0
        return rounded
    if isinstance(value, dict):
        return {str(k): normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_numbers(v) for v in value]
    return value


def canonical_dumps(value: Any) -> str:
    normalized = normalize_numbers(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def content_digest(value: Any) -> str:
    return f"sha256:{sha256_hex(value)}"


def digest_matches(value: Any, expected: str) -> bool:
    return content_digest(value) == expected
