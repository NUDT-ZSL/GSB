from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import numpy as np


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_text(canonical_json(value))


def array_bytes(array: np.ndarray) -> bytes:
    canonical = np.ascontiguousarray(array, dtype="<f8")
    return canonical.tobytes(order="C")


def array_digest(array: np.ndarray) -> str:
    shape = canonical_json(list(np.asarray(array).shape))
    return digest_bytes(b"F8LE|" + shape.encode("ascii") + b"|" + array_bytes(array))


def encode_array(array: np.ndarray) -> dict[str, Any]:
    arr = np.ascontiguousarray(array, dtype="<f8")
    return {
        "shape": list(arr.shape),
        "dtype": "<f8",
        "digest": array_digest(arr),
        "b64": base64.b64encode(arr.tobytes(order="C")).decode("ascii"),
    }


def decode_array(encoded: dict[str, Any]) -> np.ndarray:
    raw = base64.b64decode(encoded["b64"], validate=True)
    arr = np.frombuffer(raw, dtype="<f8").copy()
    arr = arr.reshape(tuple(encoded["shape"]))
    if array_digest(arr) != encoded["digest"]:
        raise ValueError("array digest mismatch")
    return arr


def array_checksum(array: np.ndarray) -> str:
    return array_digest(array)


def float_hex(value: float) -> str:
    return float(value).hex()


def stable_int(*parts: object, bits: int = 128) -> int:
    material = "|".join(str(part) for part in parts)
    return int.from_bytes(
        hashlib.sha256(material.encode("utf-8")).digest()[: bits // 8], "little"
    )


def npy_bytes(array: np.ndarray) -> bytes:
    import io

    stream = io.BytesIO()
    np.save(stream, np.ascontiguousarray(array, dtype="<f8"), allow_pickle=False)
    return stream.getvalue()

