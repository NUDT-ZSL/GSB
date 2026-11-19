"""Deterministic serialization, hashing, and primitive validation helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import BusinessError, ErrorCode


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def require_object(value: Any, field: str, code: str = ErrorCode.INVALID_SCHEMA) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, f"{field} must be a JSON object", {"field": field})
    return value


def require_list(value: Any, field: str, code: str = ErrorCode.INVALID_SCHEMA) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(code, f"{field} must be a JSON array", {"field": field})
    return value


def require_positive_int(value: Any, field: str, code: str = ErrorCode.INVALID_CLOCK) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BusinessError(code, f"{field} must be a positive integer", {"field": field})
    return value


def require_nonnegative_int(value: Any, field: str, code: str = ErrorCode.INVALID_CONFIG) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BusinessError(code, f"{field} must be a non-negative integer", {"field": field})
    return value


def require_nonempty_str(value: Any, field: str, code: str = ErrorCode.INVALID_SCHEMA) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BusinessError(code, f"{field} must be a non-empty string", {"field": field})
    return value


def parse_json_file(path: str, label: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            ErrorCode.INVALID_STATE,
            f"{label} file does not exist",
            {"path": path},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.INVALID_STATE,
            f"{label} file is not valid JSON",
            {"path": path, "line": exc.lineno, "column": exc.colno},
        ) from exc


def dedupe_preserving_order(values: Sequence[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        marker = canonical_json(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def sorted_mapping_items(value: Mapping[str, Any]) -> list[tuple[str, Any]]:
    return sorted(value.items(), key=lambda item: item[0])

