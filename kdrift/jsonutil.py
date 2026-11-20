from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .errors import EngineError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"


def read_json(path: str, *, code: str = "E_BAD_JSON") -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise EngineError(code, f"file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(code, f"cannot parse JSON file {path}: {exc}") from exc


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )

