from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import numpy as np


def canonical_json(value: Any) -> str:
    """Serialize with a stable key order and no platform-dependent formatting."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_payload(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def array_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    if contiguous.dtype.byteorder == ">":
        contiguous = contiguous.astype(contiguous.dtype.newbyteorder("<"), copy=False)
    return sha256_bytes(contiguous.tobytes(order="C"))


def dict_sha256(value: Mapping[str, Any]) -> str:
    return checksum_payload(dict(value))


class HashPRNG:
    """Counter-mode SHA-256 PRNG with a stable, explicit byte stream."""

    def __init__(self, key: str | bytes) -> None:
        material = key.encode("utf-8") if isinstance(key, str) else key
        self._key = hashlib.sha256(b"latentflow-prng-v1\x00" + material).digest()
        self._counter = 0
        self._buffer = b""

    def _block(self) -> bytes:
        counter = self._counter.to_bytes(8, "big")
        self._counter += 1
        return hashlib.sha256(self._key + counter).digest()

    def read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._buffer += self._block()
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def unit_floats(self, shape: tuple[int, ...]) -> np.ndarray:
        count = int(np.prod(shape, dtype=np.int64))
        raw = self.read(count * 8)
        words = np.frombuffer(raw, dtype="<u8")
        # 53 mantissa bits preserve the full precision of float64.
        floats = (words >> np.uint64(11)).astype(np.float64) * (1.0 / 9007199254740992.0)
        return floats.reshape(shape)

    def signed_floats(self, shape: tuple[int, ...], dtype: np.dtype[Any] = np.float32) -> np.ndarray:
        return (self.unit_floats(shape) * 2.0 - 1.0).astype(dtype)


def prompt_condition(prompt: str, size: int = 64) -> tuple[str, np.ndarray]:
    """Map a prompt to a stable condition vector via SHA-256 counter mode."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    rng = HashPRNG(b"condition\x00" + bytes.fromhex(digest))
    condition = rng.signed_floats((size,), dtype=np.float32)
    return digest, condition
