"""Small deterministic parsing and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .errors import Codes, Issue

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def canonical_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 128 or _CONTROL_RE.search(normalized):
        return None
    return normalized.casefold()


def canonical_policy_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 128 or _CONTROL_RE.search(normalized):
        return None
    return normalized


def digest_format_valid(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_nonnegative_int(value: Any) -> int | None:
    parsed = parse_int(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def parse_positive_int(value: Any) -> int | None:
    parsed = parse_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def parse_totp_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 999_999 else None
    if isinstance(value, str) and re.fullmatch(r"\d{6}", value):
        return int(value)
    return None


def parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not parsed.is_finite():
            return None
        return parsed
    return None


def decimal_ms_to_us(value: Any) -> int | None:
    parsed = parse_decimal(value)
    if parsed is None or parsed < 0:
        return None
    micros = parsed * Decimal(1000)
    if micros != micros.to_integral_value():
        return None
    return int(micros)


def seconds_to_window_ms(value: Any) -> int | None:
    parsed = parse_decimal(value)
    if parsed is None or parsed <= 0:
        return None
    millis = parsed * Decimal(1000)
    if millis != millis.to_integral_value():
        return None
    result = int(millis)
    return result if result > 0 else None


def parse_components(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    result: list[int] = []
    for item in value:
        parsed = parse_nonnegative_int(item)
        if parsed is None:
            return None
        result.append(parsed)
    return tuple(result)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_safe(value: Any) -> Any:
    """Convert common scalar containers to strict JSON-safe values."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def issue(
    source: str,
    record_index: int,
    code: str,
    message: str,
    **detail: Any,
) -> Issue:
    return Issue(source, record_index, code, message, json_safe(detail))


def sorted_reasons(values: Iterable[str]) -> list[str]:
    return sorted(set(values))

