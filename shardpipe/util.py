from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{id(data):x}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        import os

        os.fsync(handle.fileno())
    import os

    os.replace(tmp, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def stable_sorted_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: stable_sorted_dict(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [stable_sorted_dict(item) for item in value]
    return value
