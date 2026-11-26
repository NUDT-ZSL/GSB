"""Canonical serialization, stable hashing and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import DriftError, E_IO


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not accept non-finite floats")
        return stable_float(value)
    return value


def stable_float(value: float, digits: int = 12) -> float:
    """Quantize serialized values without depending on platform formatting."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite float")
    if abs(number) < 10 ** (-(digits + 2)):
        return 0.0
    return float(f"{number:.{digits}g}")


def canonical_json(value: Any, *, newline: bool = False) -> str:
    text = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text + ("\n" if newline else "")


def canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    return canonical_json(value, newline=newline).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception as exc:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        if isinstance(exc, DriftError):
            raise
        raise DriftError(E_IO, f"failed to atomically write {target}") from exc


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | Path, value: Any, *, newline: bool = True) -> None:
    atomic_write_text(path, canonical_json(value, newline=newline))


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def checked_payload(
    payload: dict[str, Any], checksum_key: str = "checksum"
) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key != checksum_key}
    body[checksum_key] = sha256_value(body)
    return body


def verify_payload_checksum(
    payload: dict[str, Any], checksum_key: str = "checksum"
) -> None:
    supplied = payload.get(checksum_key)
    body = {key: value for key, value in payload.items() if key != checksum_key}
    expected = sha256_value(body)
    if supplied != expected:
        raise DriftError(
            "E_CHECKSUM_MISMATCH",
            "content checksum mismatch",
            expected=expected,
            actual=supplied,
        )


def read_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError(E_IO, f"cannot read JSON file {path}") from exc


def flatten_mapping(
    value: dict[str, Any], prefix: str = ""
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        joined = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            result.update(flatten_mapping(item, joined))
        else:
            result[joined] = item
    return result


def unique_sorted(values: Iterable[Any]) -> list[Any]:
    return sorted(set(values))

