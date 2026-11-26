from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any, *, prefix: str = "LEDGER-JSON/v1") -> str:
    digest = hashlib.sha256()
    digest.update(prefix.encode("utf-8"))
    digest.update(b"\n")
    digest.update(canonical_json_bytes(value))
    return f"sha256:{digest.hexdigest()}"


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _i64(array: np.ndarray | list[int] | tuple[int, ...]) -> np.ndarray:
    return np.ascontiguousarray(array, dtype="<i8")


def _f64(array: np.ndarray | list[float] | tuple[float, ...]) -> np.ndarray:
    return np.ascontiguousarray(array, dtype="<f8")


def arrays_digest(
    namespace: str,
    *,
    row_ptr: np.ndarray | None = None,
    col_idx: np.ndarray | None = None,
    values: np.ndarray | None = None,
    timestamps: np.ndarray | None = None,
) -> str:
    """Hash arrays with lengths and little-endian raw storage.

    The length prefixes avoid ambiguous concatenations. Float64 bits are hashed
    directly, so the digest is stable across JSON formatting and Python runs.
    """

    digest = hashlib.sha256()
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\n")
    for name, array, converter in (
        ("row_ptr", row_ptr, _i64),
        ("col_idx", col_idx, _i64),
        ("values", values, _f64),
        ("timestamps", timestamps, _i64),
    ):
        if array is None:
            continue
        normalized = converter(array)
        digest.update(name.encode("utf-8"))
        digest.update(struct.pack("<Q", int(normalized.size)))
        digest.update(normalized.tobytes(order="C"))
    return f"sha256:{digest.hexdigest()}"


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json_atomic(path: str | Path, value: Any, *, indent: int = 2) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False) + "\n",
    )


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
