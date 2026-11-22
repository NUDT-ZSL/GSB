from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    """Stable UTF-8 JSON used for every persisted digest."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def digest_without_fields(value: dict[str, Any], fields: Iterable[str]) -> str:
    ignored = set(fields)
    body = {key: item for key, item in value.items() if key not in ignored}
    return digest_json(body)


def hash_chain(previous: str | None, event_digest: str, result_digest: str) -> str:
    return digest_json(
        {
            "previous": previous,
            "event": event_digest,
            "result": result_digest,
        }
    )


def deep_get(value: Any, path: str) -> Any:
    current = value
    if path == "":
        return current
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def flatten_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten leaf values; lists and scalars are leaves."""
    if isinstance(value, dict) and value:
        result: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten_paths(value[key], child))
        return result
    return {prefix: value}


def diff_paths(before: Any, after: Any) -> list[dict[str, Any]]:
    left = flatten_paths(before)
    right = flatten_paths(after)
    result: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        left_value = left.get(path, {"__missing__": True})
        right_value = right.get(path, {"__missing__": True})
        if left_value != right_value:
            result.append(
                {
                    "path": path,
                    "before": left_value,
                    "after": right_value,
                }
            )
    return result
