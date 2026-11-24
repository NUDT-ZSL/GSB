from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def safe_read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(parent)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n",
    )


def hash_stream(handle: BinaryIO, limit: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    remaining = limit
    total = 0
    while remaining is None or remaining > 0:
        request = 1024 * 1024
        if remaining is not None:
            request = min(request, remaining)
        chunk = handle.read(request)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return digest.hexdigest(), total


def is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def require_mapping(value: Any, code: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        from .errors import fail

        fail(code, f"{label} 必须是 JSON 对象", field=label)
    return value


def get_any(
    mapping: dict[str, Any],
    names: tuple[str, ...],
    label: str,
    *,
    required: bool = True,
) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    if required:
        from .errors import fail

        fail("E_SCHEMA", f"缺少字段 {label}", field=label, accepted_names=list(names))
    return None

