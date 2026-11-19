"""Canonical serialization and hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import fail


def _reject_constant(value: str) -> None:
    fail("INPUT_JSON_INVALID", "Non-finite JSON constant is not allowed", constant=value)


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("INPUT_JSON_INVALID", "Duplicate JSON object key", key=key)
        result[key] = value
    return result


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        fail("INPUT_SHAPE_INVALID", "Cannot read JSON file", path=str(source), reason=str(exc))
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_no_duplicates,
        )
    except json.JSONDecodeError as exc:
        fail(
            "INPUT_JSON_INVALID",
            "Invalid JSON",
            path=str(source),
            line=exc.lineno,
            column=exc.colno,
            reason=exc.msg,
        )


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        fail("INPUT_SHAPE_INVALID", "Value is not canonically serializable", reason=str(exc))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_json_output(value: Any, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    return canonical_json(value)
