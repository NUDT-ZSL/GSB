"""Canonical byte encodings, array digests, and RNG state helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Any, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_array(array: np.ndarray) -> np.ndarray:
    """Return a C-contiguous array with a platform-independent byte order."""

    value = np.asarray(array)
    if value.dtype.byteorder == ">" or (
        value.dtype.byteorder == "=" and not np.little_endian
    ):
        value = value.astype(value.dtype.newbyteorder("<"), copy=False)
    return np.ascontiguousarray(value)


def array_descriptor(array: np.ndarray) -> dict[str, Any]:
    value = canonical_array(array)
    return {"dtype": value.dtype.str, "shape": list(value.shape)}


def array_bytes(array: np.ndarray) -> bytes:
    return canonical_array(array).tobytes(order="C")


def digest_array_map(arrays: Mapping[str, np.ndarray]) -> str:
    """SHA-256 over sorted array names, dtypes/shapes, and raw C-order bytes."""

    digest = hashlib.sha256()
    for name in sorted(arrays):
        descriptor = {
            "name": name,
            **array_descriptor(arrays[name]),
        }
        digest.update(canonical_json_bytes(descriptor))
        digest.update(b"\x00")
        digest.update(array_bytes(arrays[name]))
    return digest.hexdigest()


def digest_array_map_v1(arrays: Mapping[str, np.ndarray]) -> str:
    """Legacy v1 digest: sorted names plus raw bytes, no dtype/shape framing."""

    digest = hashlib.sha256()
    for name in sorted(arrays):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(array_bytes(arrays[name]))
    return digest.hexdigest()


def arrays_equal(left: np.ndarray, right: np.ndarray) -> bool:
    left_value = canonical_array(left)
    right_value = canonical_array(right)
    return (
        left_value.dtype == right_value.dtype
        and left_value.shape == right_value.shape
        and left_value.tobytes(order="C") == right_value.tobytes(order="C")
    )


def jsonable(value: Any) -> Any:
    """Convert NumPy scalar/container values into deterministic JSON values."""

    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def rng_state_to_json(generator: np.random.Generator) -> dict[str, Any]:
    return jsonable(generator.bit_generator.state)


def generator_from_state(state: Mapping[str, Any]) -> np.random.Generator:
    bit_generator_name = str(state.get("bit_generator", "PCG64"))
    if bit_generator_name != "PCG64":
        raise ValueError(f"unsupported bit generator: {bit_generator_name}")
    generator = np.random.Generator(np.random.PCG64())
    generator.bit_generator.state = dict(state)
    return generator


def rng_state_digest(state: Mapping[str, Any]) -> str:
    return sha256_json(jsonable(state))
