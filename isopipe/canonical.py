from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{__import__('os').getpid()}")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        __import__("os").fsync(handle.fileno())
    __import__("os").replace(temp, path)


def first_difference(
    left: Any, right: Any, path: str = "$"
) -> dict[str, Any] | None:
    if type(left) is not type(right):
        return {"field": path, "serial": left, "sandbox": right}
    if isinstance(left, dict):
        left_keys = list(left)
        right_keys = list(right)
        if left_keys != right_keys:
            return {"field": f"{path}.<keys>", "serial": left_keys, "sandbox": right_keys}
        for key in left_keys:
            found = first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {"field": f"{path}.<length>", "serial": len(left), "sandbox": len(right)}
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = first_difference(left_item, right_item, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return {"field": path, "serial": left, "sandbox": right}
    return None

