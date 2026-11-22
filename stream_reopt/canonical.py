"""Canonical JSON and digest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BusinessError(
                ErrorCode.BAD_PLAN,
                f"JSON object contains duplicate key {key!r}",
                details={"key": key},
            )
        result[key] = value
    return result


def load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            value = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except BusinessError:
        raise
    except FileNotFoundError as exc:
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            f"sample file not found: {p}",
            details={"path": str(p)},
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            f"cannot parse sample JSON: {exc}",
            details={"path": str(p)},
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            ErrorCode.BAD_PLAN,
            "sample root must be a JSON object",
            details={"actual_type": type(value).__name__},
        )
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

