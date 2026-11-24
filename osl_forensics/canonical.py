"""Canonical keys and stable report serialization helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


UINT64_MAX = (1 << 64) - 1


def escape_name(name: str) -> str:
    """JSON-Pointer-like escaping so '/' and '~' remain unambiguous."""

    return name.replace("~", "~0").replace("/", "~1")


def unescape_name(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def segment_key(path_names: Iterable[str] | str) -> str:
    if isinstance(path_names, str):
        path_names = (path_names,)
    return "/" + "/".join(escape_name(name) for name in path_names)


def stable_json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_json_bytes(value: Any) -> bytes:
    return stable_json_dumps(value).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (set, tuple)):
        return sorted(obj) if obj and all(isinstance(x, str) for x in obj) else list(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")
