from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import ValidationError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def read_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(
            "E_BAD_JSON",
            f"cannot read JSON file: {source}",
            {"path": str(source), "error": str(exc)},
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "E_BAD_JSON",
            f"invalid JSON: {source}",
            {
                "path": str(source),
                "line": exc.lineno,
                "column": exc.colno,
                "error": exc.msg,
            },
        ) from exc
