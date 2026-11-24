from __future__ import annotations

import math
from typing import Any

from .errors import AnimRecalcError


def fail(code: str, message: str, **details: Any) -> AnimRecalcError:
    return AnimRecalcError(code, message, details)


def require_dict(value: Any, label: str, code: str = "E_SCHEMA") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail(code, f"{label} must be an object", received_type=type(value).__name__)
    return value


def require_list(value: Any, label: str, code: str = "E_SCHEMA") -> list[Any]:
    if not isinstance(value, list):
        raise fail(code, f"{label} must be an array", received_type=type(value).__name__)
    return value


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) or (
        hasattr(value, "dtype") and getattr(value, "shape", None) == ()
    )


def finite_number(value: Any, label: str) -> float:
    if not is_number(value):
        raise fail("E_NONFINITE", f"{label} must be a finite number", value=value)
    number = float(value)
    if not math.isfinite(number):
        raise fail("E_NONFINITE", f"{label} must be finite", value=value)
    return number


def require_nonnegative_int(value: Any, label: str) -> int:
    if not is_int(value) or value < 0:
        raise fail("E_KEY_TIME", f"{label} must be a nonnegative integer", value=value)
    return int(value)


def require_positive_int(value: Any, label: str) -> int:
    if not is_int(value) or value <= 0:
        raise fail("E_SCHEMA", f"{label} must be a positive integer", value=value)
    return int(value)


def finite_vector(value: Any, size: int, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise fail(
            "E_NONFINITE",
            f"{label} must be an array of {size} finite numbers",
            value=value,
        )
    return [finite_number(item, f"{label}[{index}]") for index, item in enumerate(value)]


def normalize_channel_name(channel: str) -> str:
    aliases = {
        "location": "translation",
        "position": "translation",
        "pos": "translation",
        "quaternion": "rotation",
        "quat": "rotation",
        "scaling": "scale",
        "scale": "scale",
    }
    return aliases.get(channel, channel)
