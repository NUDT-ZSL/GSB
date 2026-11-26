from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import threading
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from quota_ledger.errors import fail

MONEY_SCALE = 1_000_000


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("E_DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail("E_IO", f"cannot read JSON file: {exc}", str(path))
    try:
        return json.loads(
            text,
            parse_constant=lambda token: fail(
                "E_NONFINITE_NUMBER", f"non-finite JSON number: {token}", str(path)
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        fail("E_INVALID_JSON", f"invalid JSON: {exc.msg}", f"{path}:{exc.lineno}:{exc.colno}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_dict(value: Any, path: str, label: str = "object") -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("E_TYPE", f"{label} must be an object", path)
    return value


def require_list(value: Any, path: str, label: str = "array") -> list[Any]:
    if not isinstance(value, list):
        fail("E_TYPE", f"{label} must be an array", path)
    return value


def require_int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail("E_BAD_INTEGER", "value must be an integer", path)
    if minimum is not None and value < minimum:
        fail("E_OUT_OF_RANGE", f"integer must be >= {minimum}", path, actual=value)
    if maximum is not None and value > maximum:
        fail("E_OUT_OF_RANGE", f"integer must be <= {maximum}", path, actual=value)
    return value


def require_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        fail("E_TYPE", "value must be a string", path)
    if nonempty and not value.strip():
        fail("E_EMPTY_VALUE", "value must not be empty", path)
    return value


def parse_decimal(
    value: Any,
    path: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        fail("E_BAD_NUMBER", "value must be a finite decimal number", path)
    if isinstance(value, float) and not math.isfinite(value):
        fail("E_NONFINITE_NUMBER", "number must be finite", path)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            fail("E_BAD_NUMBER", "empty numeric string", path)
        try:
            result = Decimal(text)
        except InvalidOperation:
            fail("E_BAD_NUMBER", "invalid decimal number", path, actual=value)
    elif isinstance(value, (int, float, Decimal)):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            fail("E_BAD_NUMBER", "invalid decimal number", path, actual=str(value))
    else:
        fail("E_BAD_NUMBER", "value must be a JSON number or numeric string", path)
    if not result.is_finite():
        fail("E_NONFINITE_NUMBER", "number must be finite", path)
    if positive and result <= 0:
        fail("E_NON_POSITIVE", "number must be > 0", path, actual=str(result))
    if nonnegative and result < 0:
        fail("E_NEGATIVE", "number must be >= 0", path, actual=str(result))
    return result


def quota_micros(
    value: Any,
    path: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    direction: str = "up",
) -> tuple[int, int, str]:
    """Convert a quota to micro-units.

    Returns (micros, rounding_error_micros, rounding_direction).
    The internal scale is always 1e6.  A positive quota is rounded up so a
    reservation can never undersell; non-negative actual usage is rounded down.
    """

    decimal = parse_decimal(value, path, positive=positive, nonnegative=nonnegative)
    scaled = decimal * MONEY_SCALE
    if direction == "up":
        integral = int(scaled.to_integral_value(rounding=ROUND_CEILING))
    else:
        integral = int(scaled.to_integral_value(rounding=ROUND_FLOOR))
    error = integral - scaled
    if error == 0:
        rounding = "none"
    elif error > 0:
        rounding = "up"
    else:
        rounding = "down"
    error_micros = int(abs(error).to_integral_value(rounding=ROUND_CEILING))
    if positive and integral <= 0:
        fail("E_QUOTA_BELOW_PRECISION", "positive quota rounds to zero at scale 1e6", path)
    if nonnegative and integral < 0:
        fail("E_NEGATIVE", "quota must be >= 0", path)
    return integral, error_micros, rounding


def ratio_fraction(value: Any, path: str) -> tuple[Fraction, str]:
    decimal = parse_decimal(value, path, positive=True)
    return Fraction(decimal), format(decimal.normalize(), "f")


def ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def micros_to_quota(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    absolute = abs(micros)
    whole, fraction = divmod(absolute, MONEY_SCALE)
    if fraction == 0:
        return f"{sign}{whole}"
    result = f"{sign}{whole}.{fraction:06d}".rstrip("0")
    return result


def ratio_text(fraction: Fraction) -> str:
    decimal = Decimal(fraction.numerator) / Decimal(fraction.denominator)
    return format(decimal.quantize(Decimal("0.000001")), "f")


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(key, threading.Lock())
    with thread_lock:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

