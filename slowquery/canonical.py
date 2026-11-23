"""Small deterministic serialization and validation helpers."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

from .errors import EngineError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_round(value: float, places: int = 12) -> float:
    rounded = round(float(value), places)
    if rounded == 0:
        return 0.0
    return rounded


def require_mapping(value: Any, code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineError(code, f"{path} must be an object", {"path": path})
    return value


def require_list(value: Any, code: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise EngineError(code, f"{path} must be an array", {"path": path})
    return value


def require_nonempty_str(value: Any, code: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineError(code, f"{path} must be a non-empty string", {"path": path})
    return value


def require_int(
    value: Any,
    code: str,
    path: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(code, f"{path} must be an integer", {"path": path})
    if minimum is not None and value < minimum:
        raise EngineError(
            code,
            f"{path} must be >= {minimum}",
            {"path": path, "minimum": minimum, "value": value},
        )
    return value


def require_number(
    value: Any,
    code: str,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineError(code, f"{path} must be a number", {"path": path})
    number = float(value)
    if not math.isfinite(number):
        raise EngineError(code, f"{path} must be finite", {"path": path})
    if minimum is not None and number < minimum:
        raise EngineError(
            code,
            f"{path} must be >= {minimum}",
            {"path": path, "minimum": minimum, "value": number},
        )
    if maximum is not None and number > maximum:
        raise EngineError(
            code,
            f"{path} must be <= {maximum}",
            {"path": path, "maximum": maximum, "value": number},
        )
    return number


def sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def deep_copy_json(value: Any) -> Any:
    return json.loads(canonical_json(value))
