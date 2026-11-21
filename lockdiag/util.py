"""Small deterministic helpers shared by the engine and reports."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import DiagnosticError, E_BAD_TOLERANCE

MICRO_PER_MS = 1_000_000


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_positive_int(value: Any, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DiagnosticError(code, f"{field} must be a positive integer", {"field": field})
    return value


def parse_positive_decimal_ms(value: Any, field: str = "wait_tolerance_ms") -> Decimal:
    if isinstance(value, bool):
        raise DiagnosticError(E_BAD_TOLERANCE, f"{field} must be positive", {"field": field})
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DiagnosticError(E_BAD_TOLERANCE, f"{field} must be a decimal number", {"field": field})
    if not number.is_finite() or number <= 0:
        raise DiagnosticError(E_BAD_TOLERANCE, f"{field} must be positive", {"field": field})
    return number


def decimal_to_micro(value: Decimal) -> int:
    return int((value * MICRO_PER_MS).to_integral_value(rounding="ROUND_HALF_UP"))


def tolerance_to_units(value: Decimal, scale: int) -> int:
    return int((value * (10**scale)).to_integral_value(rounding="ROUND_HALF_UP"))


def format_units(units: int, scale: int) -> str:
    factor = 10**scale
    sign = "-" if units < 0 else ""
    units = abs(units)
    whole, frac = divmod(units, factor)
    if scale == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:0{scale}d}"


def format_micro(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    micro = abs(micro)
    whole, frac = divmod(micro, MICRO_PER_MS)
    return f"{sign}{whole}.{frac:06d}"


def rational_micro(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    raw = numerator * MICRO_PER_MS
    if raw >= 0:
        return (raw + denominator // 2) // denominator
    return -((-raw + denominator // 2) // denominator)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise DiagnosticError(E_BAD_JSON, f"JSON file not found: {path}", {"path": str(path)})
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            E_BAD_JSON,
            f"invalid JSON in {path}: {exc.msg}",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(pretty_json(value))
        handle.write("\n")
