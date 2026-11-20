from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, fail


class DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON key: {key}")
        self.key = key


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def load_json(path: str | Path) -> Any:
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        fail("E_BAD_INPUT", "cannot read JSON file", path=str(target), reason=str(exc))
    try:
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except DuplicateJsonKey as exc:
        raise BusinessError(
            code="E_ATTRIBUTE_TYPE_CONFLICT",
            message="duplicate attribute key has conflicting values",
            details={"path": str(target), "key": exc.key},
        ) from exc
    except json.JSONDecodeError as exc:
        fail(
            "E_BAD_INPUT",
            "invalid JSON",
            path=str(target),
            line=exc.lineno,
            column=exc.colno,
            reason=exc.msg,
        )


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


def require_dict(value: Any, label: str, code: str = "E_BAD_INPUT") -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, f"{label} must be an object", field=label)
    return value


def require_list(value: Any, label: str, code: str = "E_BAD_INPUT") -> list[Any]:
    if not isinstance(value, list):
        fail(code, f"{label} must be an array", field=label)
    return value


def require_str(value: Any, label: str, code: str = "E_BAD_INPUT") -> str:
    if not isinstance(value, str) or not value.strip():
        fail(code, f"{label} must be a non-empty string", field=label, value=value)
    return value


def require_int(value: Any, label: str, code: str = "E_BAD_INPUT") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(code, f"{label} must be an integer", field=label, value=value)
    return value


def require_number(value: Any, label: str, code: str = "E_BAD_INPUT") -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(code, f"{label} must be a finite number", field=label, value=value)
    number = float(value)
    if not math.isfinite(number):
        fail(code, f"{label} must be a finite number", field=label, value=value)
    return value


def normalize_number(value: float) -> int | float:
    rounded = round(float(value), 6)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


@contextlib.contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def decision_second(logical_ms: int) -> int:
    return logical_ms // 1000

