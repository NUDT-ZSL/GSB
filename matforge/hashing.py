from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_obj(value: Any) -> str:
    return digest_bytes(canonical_json(value))


def digest_text(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


def array_digest(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    header = f"{contiguous.dtype.str}|{contiguous.shape}|".encode("ascii")
    return digest_bytes(header + contiguous.tobytes(order="C"))


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def combine_digests(*parts: str) -> str:
    return digest_text("|".join(parts))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{hashlib.sha256(payload).hexdigest()[:10]}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        try:
            import os

            os.fsync(handle.fileno())
        except OSError:
            pass
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")

