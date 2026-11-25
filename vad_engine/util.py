"""Deterministic serialization, hashing, parsing, and file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import E_FORMAT_CORRUPT, E_PARSE_ERROR, EngineError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def quantize(value: float, places: int = 12) -> float:
    if not math.isfinite(value):
        raise EngineError(E_PARSE_ERROR, "non-finite float cannot be serialized")
    rounded = round(float(value), places)
    return 0.0 if rounded == 0 else rounded


def parse_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise EngineError(E_PARSE_ERROR, f"{field} must be an integer", {"field": field})
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError("not integral")
            parsed = int(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text or any(ch not in "+-0123456789" for ch in text):
                raise ValueError("not an integer")
            parsed = int(text, 10)
        else:
            raise ValueError("unsupported type")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} is not a parsable integer",
            {"field": field, "value": value},
        ) from exc
    if minimum is not None and parsed < minimum:
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} is below minimum {minimum}",
            {"field": field, "value": parsed, "minimum": minimum},
        )
    return parsed


def parse_float(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise EngineError(E_PARSE_ERROR, f"{field} must be numeric", {"field": field})
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} is not parsable as a number",
            {"field": field, "value": value},
        ) from exc
    if not math.isfinite(parsed):
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} must be finite",
            {"field": field, "value": value},
        )
    if minimum is not None and parsed < minimum:
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} is below minimum {minimum}",
            {"field": field, "value": parsed, "minimum": minimum},
        )
    if maximum is not None and parsed > maximum:
        raise EngineError(
            E_PARSE_ERROR,
            f"{field} is above maximum {maximum}",
            {"field": field, "value": parsed, "maximum": maximum},
        )
    return parsed


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineError(E_FORMAT_CORRUPT, f"{field} must be an object", {"field": field})
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise EngineError(E_FORMAT_CORRUPT, f"{field} must be an array", {"field": field})
    return value


def require_field(mapping: dict[str, Any], field: str, context: str) -> Any:
    if field not in mapping:
        raise EngineError(
            E_FORMAT_CORRUPT,
            f"missing required field {field}",
            {"context": context, "field": field},
        )
    return mapping[field]


def seconds_to_ms(value: Any, field: str) -> int:
    number = parse_float(value, field, minimum=0.0)
    try:
        milliseconds = int(
            (Decimal(str(number)) * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP)
        )
    except InvalidOperation as exc:
        raise EngineError(E_PARSE_ERROR, f"{field} cannot be converted", {"field": field}) from exc
    return milliseconds


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        _fsync_directory(destination.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_json_file(path: str | Path, *, context: str) -> Any:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise EngineError(
            E_FORMAT_CORRUPT,
            f"cannot read {context}",
            {"path": str(path), "error": str(exc)},
        ) from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EngineError(
            E_PARSE_ERROR,
            f"invalid JSON in {context}",
            {"path": str(path), "error": str(exc)},
        ) from exc


def fixed_from_float(value: float, scale: int = 1_000_000) -> int:
    return int(round(float(value) * scale))


def fixed_to_float(value: int, scale: int = 1_000_000) -> float:
    return value / scale


def range_to_dict(start: int, end: int) -> dict[str, int]:
    return {"start": start, "end": end, "count": max(0, end - start)}
