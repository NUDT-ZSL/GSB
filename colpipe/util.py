"""Deterministic JSON, hashing, file replacement and scalar helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    kwargs: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if pretty:
        kwargs["indent"] = 2
        return json.dumps(value, **kwargs) + "\n"
    kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def write_json(path: str | Path, value: Any) -> None:
    _atomic_write(Path(path), canonical_json(value, pretty=True).encode("utf-8"))


def write_bytes(path: str | Path, value: bytes) -> None:
    _atomic_write(Path(path), value)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def type_tag(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int64"
    if isinstance(value, float):
        return "float64"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"unsupported scalar type: {type(value)!r}")


def scalar_key(value: Any) -> tuple[int, Any]:
    """A total, deterministic ordering key for supported scalar values."""

    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, value)
    if isinstance(value, float):
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    raise TypeError(f"unsupported scalar type: {type(value)!r}")


def group_key(values: Iterable[Any]) -> tuple[tuple[str, Any], ...]:
    return tuple((type_tag(value), value) for value in values)


def json_value(value: Any) -> Any:
    """Return a JSON-safe representation without changing scalar values."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported JSON value: {type(value)!r}")

