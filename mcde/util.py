from __future__ import annotations

import hashlib
import json
import os
import time
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


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def wall_ms() -> int:
    return int(time.time() * 1000)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        from .errors import TypeMismatchError

        raise TypeMismatchError(
            f"{where} must be an object",
            where=where,
            expected="object",
            actual=type(value).__name__,
        )
    return value


def require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        from .errors import TypeMismatchError

        raise TypeMismatchError(
            f"{where} must be an array",
            where=where,
            expected="array",
            actual=type(value).__name__,
        )
    return value


def require_str(value: Any, where: str, *, nonempty: bool = True) -> str:
    from .errors import TypeMismatchError

    if not isinstance(value, str):
        raise TypeMismatchError(
            f"{where} must be a string",
            where=where,
            expected="string",
            actual=type(value).__name__,
        )
    if nonempty and not value.strip():
        raise TypeMismatchError(
            f"{where} must not be empty",
            where=where,
            expected="non-empty string",
            actual="empty string",
        )
    return value


def require_bool(value: Any, where: str) -> bool:
    from .errors import TypeMismatchError

    if type(value) is not bool:
        raise TypeMismatchError(
            f"{where} must be a boolean",
            where=where,
            expected="boolean",
            actual=type(value).__name__,
        )
    return value


def require_int(
    value: Any,
    where: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> int:
    from .errors import LimitError, TypeMismatchError

    if type(value) is not int:
        raise TypeMismatchError(
            f"{where} must be an integer",
            where=where,
            expected="integer",
            actual=type(value).__name__,
        )
    if positive and value <= 0:
        raise LimitError(
            f"{where} must be a positive integer",
            where=where,
            value=value,
            rule="> 0",
        )
    if nonnegative and value < 0:
        raise LimitError(
            f"{where} must be a non-negative integer",
            where=where,
            value=value,
            rule=">= 0",
        )
    return value


def file_uri(path: Path) -> str:
    return os.fspath(path.resolve())

