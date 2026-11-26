"""Small deterministic and validation helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    INVALID_JSON,
    INVALID_JSONL,
    BusinessError,
    fail,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_obj(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seeded_digest(*parts: Any) -> str:
    """Hash a domain-separated sequence of JSON values."""
    return digest_obj({"parts": list(parts)})


def stable_json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def read_json_file(path: str | os.PathLike[str], *, code: str = INVALID_JSON) -> Any:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            code,
            f"cannot read JSON file: {source}",
            details={"path": str(source), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BusinessError(
            code,
            f"invalid JSON in {source}",
            details={
                "path": str(source),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "colno", None),
                "reason": str(exc),
            },
        ) from exc


def read_json_or_jsonl(path: str | os.PathLike[str], *, kind: str) -> Any:
    """Read a JSON document or a JSONL document.

    An array, or an object with the conventional ``entries``/``records`` key,
    is returned as a list.  A single JSON object is returned as a one-item
    list.  Blank JSONL lines are ignored.
    """

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            INVALID_JSONL if kind == "jsonl" else INVALID_JSON,
            f"cannot read {kind} file: {source}",
            details={"path": str(source), "reason": str(exc)},
        ) from exc

    stripped = text.strip()
    if not stripped:
        fail(
            INVALID_JSONL if kind == "jsonl" else INVALID_JSON,
            f"{kind} file is empty",
            path=str(source),
        )

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if parsed is not None:
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("entries", "records", "rows", "items"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
        return parsed

    records: list[Any] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError) as exc:
            raise BusinessError(
                INVALID_JSONL,
                f"invalid JSON on line {line_no} in {source}",
                details={
                    "path": str(source),
                    "line": line_no,
                    "reason": str(exc),
                },
            ) from exc
    if not records:
        fail(
            INVALID_JSONL,
            f"JSONL file has no records: {source}",
            path=str(source),
        )
    return records


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_object(value: Any, code: str, message: str, **details: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, message, **details)
    return value


def require_list(value: Any, code: str, message: str, **details: Any) -> list[Any]:
    if not isinstance(value, list):
        fail(code, message, **details)
    return value


def require_nonempty_str(
    value: Any, code: str, message: str, **details: Any
) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(code, message, **details)
    return value


def require_int(
    value: Any,
    code: str,
    message: str,
    *,
    minimum: int | None = None,
    **details: Any,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(code, message, **details)
    if minimum is not None and value < minimum:
        fail(code, message, **details)
    return value


def require_finite_number(
    value: Any,
    code: str,
    message: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    **details: Any,
) -> float:
    if not is_number(value) or not math.isfinite(float(value)):
        fail(code, message, **details)
    number = float(value)
    if minimum is not None and number < minimum:
        fail(code, message, **details)
    if maximum is not None and number > maximum:
        fail(code, message, **details)
    return number


def require_bool(value: Any, code: str, message: str, **details: Any) -> bool:
    if not isinstance(value, bool):
        fail(code, message, **details)
    return value


def require_keys(
    obj: dict[str, Any], keys: Iterable[str], code: str, message: str, **details: Any
) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        fail(code, message, missing=missing, **details)


def sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))

