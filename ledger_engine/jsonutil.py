from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_float=Decimal, parse_int=int)
    except FileNotFoundError:
        from .errors import LedgerError

        raise LedgerError("E_BAD_INPUT", f"input file not found: {source}") from None
    except json.JSONDecodeError as exc:
        from .errors import LedgerError

        raise LedgerError(
            "E_BAD_JSON",
            f"invalid JSON in {source}",
            {"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, Fraction):
        return canonical_decimal(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_dumps(value: Any, *, pretty: bool = True) -> str:
    if pretty:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=_json_default,
            )
            + "\n"
        )
    return canonical_json(value) + "\n"


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_text_atomic(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def fraction_from(value: Any, *, field: str = "value") -> Fraction:
    from .errors import LedgerError

    if isinstance(value, bool):
        raise LedgerError("E_BAD_PARAM", f"{field} must be a finite number")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerError("E_BAD_PARAM", f"{field} must be finite")
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerError("E_BAD_PARAM", f"{field} must not be empty")
        try:
            parsed = Decimal(text)
        except Exception:
            raise LedgerError("E_BAD_PARAM", f"{field} is not a decimal number") from None
        if not parsed.is_finite():
            raise LedgerError("E_BAD_PARAM", f"{field} must be finite")
        return Fraction(parsed)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LedgerError("E_BAD_PARAM", f"{field} must be finite")
        return Fraction(Decimal(str(value)))
    raise LedgerError("E_BAD_PARAM", f"{field} must be a finite number")


def canonical_decimal(value: Decimal | Fraction, precision: int = 200) -> str:
    if isinstance(value, Fraction):
        with localcontext() as context:
            context.prec = max(precision, 32)
            value = Decimal(value.numerator) / Decimal(value.denominator)
    if not value.is_finite():
        return str(value)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def decimal_from_fraction(value: Fraction, precision: int) -> Decimal:
    with localcontext() as context:
        context.prec = precision
        return Decimal(value.numerator) / Decimal(value.denominator)


def quantize_decimal(value: Decimal, precision: int) -> Decimal:
    with localcontext() as context:
        context.prec = precision
        return +value


def fraction_string(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def parse_fraction_string(value: str) -> Fraction:
    numerator, denominator = value.split("/", 1)
    return Fraction(int(numerator), int(denominator))
