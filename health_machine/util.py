"""Deterministic JSON, hashing and numeric helpers."""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import InputParseError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=json_default,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def write_json(path: str | Path, value: Any, *, pretty: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=json_default) + "\n"
    else:
        text = canonical_json(value) + "\n"
    path.write_text(text, encoding="utf-8")


def read_json(path: str | Path) -> Any:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal)
    except OSError as exc:
        raise InputParseError(
            f"cannot read JSON input {path}: {exc}",
            location={"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise InputParseError(
            f"invalid JSON in {path}: {exc.msg} at line {exc.lineno} column {exc.colno}",
            location={"path": f"{path}:{exc.lineno}:{exc.colno}"},
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc


def is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, Decimal)):
        try:
            decimal_value = Decimal(value) if not isinstance(value, Decimal) else value
            return decimal_value.is_finite()
        except (InvalidOperation, ValueError):
            return False
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def decimal_number(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def decimal_to_json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def floor_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ZeroDivisionError("denominator must be positive")
    return numerator // denominator
