from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .errors import fail


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except Exception:
        fail("E_BASE64_INVALID", "base64 data is invalid")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(
                "E_CONFIG_DUPLICATE_KEY",
                "JSON object contains a duplicate key",
                details={"key": key},
            )
        result[key] = value
    return result


def load_json_strict(path: str | Path) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(
            "E_CONFIG_READ",
            "cannot read JSON file",
            details={"path": str(path), "reason": str(exc)},
        )
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_object)
    except json.JSONDecodeError as exc:
        fail(
            "E_CONFIG_JSON",
            "file is not valid JSON",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        )


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_object)
    except json.JSONDecodeError as exc:
        fail(
            "E_CONFIG_JSON",
            "request body is not valid JSON",
            details={"line": exc.lineno, "column": exc.colno},
        )


def require_int(value: Any, code: str, field_name: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        fail(code, f"{field_name} must be an integer", details={"field": field_name})
    if minimum is not None and value < minimum:
        fail(
            code,
            f"{field_name} must be >= {minimum}",
            details={"field": field_name, "value": value, "minimum": minimum},
        )
    return value


def require_number(
    value: Any,
    code: str,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        fail(code, f"{field_name} must be a finite number", details={"field": field_name})
    number = float(value)
    if minimum is not None and number < minimum:
        fail(
            code,
            f"{field_name} must be >= {minimum}",
            details={"field": field_name, "value": number, "minimum": minimum},
        )
    if maximum is not None and number > maximum:
        fail(
            code,
            f"{field_name} must be <= {maximum}",
            details={"field": field_name, "value": number, "maximum": maximum},
        )
    return number

