"""Canonical JSON encoding and hashing.

Numbers parsed as ``Decimal`` keep their source precision.  The canonical
encoder emits JSON number literals (never strings), sorts object keys, and uses
one stable representation for every value.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .errors import ControlPlaneError


def _decimal_literal(value: Decimal) -> str:
    if not value.is_finite():
        raise ControlPlaneError("E_INVALID_NUMBER", "non-finite decimal number")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonical_json(value: Any) -> str:
    """Return a deterministic compact JSON representation."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return _decimal_literal(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ControlPlaneError("E_INVALID_NUMBER", "non-finite float")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ControlPlaneError(
                    "E_CANONICAL_KEY", "canonical JSON object keys must be strings"
                )
            parts.append(
                json.dumps(key, ensure_ascii=False) + ":" + canonical_json(value[key])
            )
        return "{" + ",".join(parts) + "}"
    raise ControlPlaneError(
        "E_CANONICAL_TYPE", f"unsupported canonical JSON type: {type(value).__name__}"
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_digest(value: Any, length: int = 16) -> str:
    return sha256_hex(value)[:length]


def pretty_json(value: Any) -> str:
    """Pretty JSON for CLI output; keys and sequences are already stable."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
