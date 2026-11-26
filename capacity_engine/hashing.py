from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def _encode_canonical(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite numbers are not canonicalizable")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=lambda item: str(item)):
            parts.append(
                json.dumps(str(key), ensure_ascii=False) + ":" + _encode_canonical(value[key])
            )
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode_canonical(item) for item in value) + "]"
    raise TypeError(f"cannot canonicalize {type(value)!r}")


def canonical_dumps(value: Any) -> str:
    return _encode_canonical(value)


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_object(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
