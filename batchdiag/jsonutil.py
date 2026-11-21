from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BusinessError


def read_json(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            "E_IO", "JSON input file does not exist", {"path": str(path)}
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_JSON_PARSE",
            "JSON input is malformed",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            "E_IO", "could not read JSON input", {"path": str(path), "error": str(exc)}
        ) from exc


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


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def decimal_string(value: Decimal) -> str:
    """Stable, non-scientific decimal string without needless trailing zeroes."""
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fixed_string(scaled: int, scale: int) -> str:
    if scale <= 0:
        raise ValueError("fixed-point scale must be a positive power of ten")
    width = 0
    probe = scale
    while probe > 1:
        if probe % 10:
            raise ValueError("fixed-point scale must be a positive power of ten")
        probe //= 10
        width += 1
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(width + 1, "0")
    if width == 0:
        return f"{sign}{abs(scaled)}"
    integer, fraction = digits[:-width], digits[-width:]
    return f"{sign}{integer}.{fraction}"


def float_string(value: float) -> str:
    # repr is shortest-round-trip and therefore deterministic for a given bit pattern.
    return repr(float(value))
