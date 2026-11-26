from __future__ import annotations

import math
import re
from typing import Any

from .canonical import canonical_json
from .config import Rules
from .errors import (
    E_CONFIDENCE_OUT_OF_RANGE,
    E_FORMAT_CORRUPT,
    E_SLOT_KEY_INVALID,
    fail,
)


def validate_session_id(value: Any, field: str = "session_id") -> str:
    if not isinstance(value, str) or not value.strip():
        fail(E_FORMAT_CORRUPT, "session_id must be a non-empty string", field=field)
    return value


def validate_slot_key(value: Any, rules: Rules, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(rules.slot_key_pattern, value) is None:
        fail(
            E_SLOT_KEY_INVALID,
            "slot_key does not match the configured naming rule",
            field=field,
            details={"value": value, "pattern": rules.slot_key_pattern},
        )
    return value


def validate_confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(
            E_FORMAT_CORRUPT,
            "confidence must be numeric",
            field=field,
        )
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        fail(
            E_FORMAT_CORRUPT,
            "confidence must be finite",
            field=field,
        )
    if number < 0.0 or number > 1.0:
        fail(
            E_CONFIDENCE_OUT_OF_RANGE,
            "confidence must be within [0, 1]",
            field=field,
            details={"value": number},
        )
    return number


def validate_turn_no(value: Any, field: str = "turn_no") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(E_FORMAT_CORRUPT, "turn_no must be a positive integer", field=field)
    return value


def validate_json_value(value: Any, field: str) -> Any:
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        fail(
            E_FORMAT_CORRUPT,
            f"value is not strict JSON: {exc}",
            field=field,
        )
    return value


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(E_FORMAT_CORRUPT, "expected a JSON object", field=field)
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        fail(E_FORMAT_CORRUPT, "expected a JSON array", field=field)
    return value


def require_int(value: Any, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(E_FORMAT_CORRUPT, "expected an integer", field=field)
    if minimum is not None and value < minimum:
        fail(E_FORMAT_CORRUPT, f"expected an integer >= {minimum}", field=field)
    return value

