"""Small deterministic utilities shared by all modules."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .errors import (
    INVALID_JSON,
    SCHEMA_ERROR,
    ControlError,
)

DECIMAL_CONTEXT_PRECISION = 60
ZERO = Decimal(0)
ONE = Decimal(1)


def dec(value: Any, field: str = "value") -> Decimal:
    """Parse a JSON scalar as Decimal without a binary-float round trip."""

    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ControlError(SCHEMA_ERROR, f"{field} must be numeric", {"field": field})
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # Decimal(str(...)) preserves the JSON-visible decimal value.
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ControlError(
                SCHEMA_ERROR, f"{field} is not a valid decimal", {"field": field}
            ) from exc
    else:
        raise ControlError(
            SCHEMA_ERROR,
            f"{field} must be a number or decimal string",
            {"field": field, "actual_type": type(value).__name__},
        )
    if not result.is_finite():
        raise ControlError(
            SCHEMA_ERROR, f"{field} must be finite", {"field": field}
        )
    return result


def integer(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlError(
            SCHEMA_ERROR,
            f"{field} must be an integer",
            {"field": field, "actual_type": type(value).__name__},
        )
    if positive and value <= 0:
        raise ControlError(
            INVALID_JSON if field.startswith("sample") else "INVALID_POSITIVE_INTEGER",
            f"{field} must be a positive integer",
            {"field": field, "value": value},
        )
    if nonnegative and value < 0:
        raise ControlError(
            SCHEMA_ERROR,
            f"{field} must be non-negative",
            {"field": field, "value": value},
        )
    return value


def decimal_text(value: Decimal, *, trim: bool = True) -> str:
    """Canonical decimal serialisation.

    We avoid ``str(float)`` and scientific notation in state snapshots.  A
    trailing zero is removed only when it does not change the value.
    """

    if not value.is_finite():
        return str(value)
    text = format(value, "f")
    if "." in text and trim:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        return "0"
    return text


def decimal_or_zero(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    return dec(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def jsonable(value: Any) -> Any:
    """Recursively convert Decimals into canonical strings."""

    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def load_json_file(path: str | os.PathLike[str]) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal, parse_int=int)
    except FileNotFoundError as exc:
        raise ControlError(
            INVALID_JSON, f"JSON file not found: {source}", {"path": str(source)}
        ) from exc
    except json.JSONDecodeError as exc:
        raise ControlError(
            INVALID_JSON,
            f"invalid JSON in {source}: line {exc.lineno} column {exc.colno}",
            {"path": str(source), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise ControlError(
            INVALID_JSON, f"cannot read {source}: {exc}", {"path": str(source)}
        ) from exc


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(pretty_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o644)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_decimal(context_precision: int = DECIMAL_CONTEXT_PRECISION):
    return localcontext() if context_precision is None else localcontext(
        prec=context_precision
    )


def ceil_decimal(value: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return int(math.ceil(value))


def floor_decimal(value: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return int(math.floor(value))


def quantized(value: Decimal, places: int = 18) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = DECIMAL_CONTEXT_PRECISION
        return value.quantize(Decimal(1).scaleb(-places))
