from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import BusinessError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json_object(path: str | os.PathLike[str], *, kind: str) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            "INPUT_JSON_INVALID",
            f"无法读取 {kind} 文件",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "INPUT_JSON_INVALID",
            f"{kind} 不是合法 JSON",
            {
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            "INPUT_JSON_INVALID",
            f"{kind} JSON 根必须是对象",
            {"path": str(path), "actual_type": type(value).__name__},
        )
    return value


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def safe_relative_path(path: str) -> bool:
    candidate = Path(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts
