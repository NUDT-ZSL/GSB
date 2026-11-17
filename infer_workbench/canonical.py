"""Canonical serialization, hashing, and bit-level helpers."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    else:
        text = canonical_json(value)
    return (text + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_object(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def digest_float32_sequence(parts: Iterable[tuple[str, np.ndarray]]) -> str:
    """Digest named float32 tensors using shape and little-endian IEEE bits."""

    digest = hashlib.sha256()
    for name, array in parts:
        encoded_name = name.encode("utf-8")
        digest.update(struct.pack("<I", len(encoded_name)))
        digest.update(encoded_name)
        tensor = np.ascontiguousarray(np.asarray(array, dtype="<f4"))
        digest.update(struct.pack("<I", tensor.ndim))
        for size in tensor.shape:
            digest.update(struct.pack("<Q", int(size)))
        digest.update(tensor.tobytes(order="C"))
    return digest.hexdigest()


def float32_bits(value: float) -> str:
    return struct.pack("<f", np.float32(value)).hex()


def float64_bits(value: float) -> str:
    return struct.pack("<d", float(value)).hex()


def bits_equal(left: Any, right: Any) -> bool:
    """Compare floats bit-for-bit and all other JSON scalars by exact value."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, float) or isinstance(right, float):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        return float64_bits(float(left)) == float64_bits(float(right))
    return left == right


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o644)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def atomic_write_json(path: str | Path, value: Any) -> Path:
    return atomic_write_bytes(path, json_bytes(value, pretty=True))


def relative_path(path: str | Path, root: str | Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def stable_max(items: Sequence[float], default: float = 0.0) -> float:
    return max((float(item) for item in items), default=default)
