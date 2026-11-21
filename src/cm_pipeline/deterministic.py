from __future__ import annotations

import hashlib
from functools import lru_cache

import numpy as np


def _stream(tag: str, count: int) -> bytes:
    if count < 0:
        raise ValueError("count must be non-negative")
    tag_bytes = tag.encode("utf-8")
    chunks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < count:
        digest = hashlib.sha256(
            b"CMP-DET-v1\x00"
            + tag_bytes
            + b"\x00"
            + counter.to_bytes(8, "big")
        ).digest()
        chunks.append(digest)
        produced += len(digest)
        counter += 1
    return b"".join(chunks)[:count]


def deterministic_bytes(tag: str, count: int) -> bytes:
    return _stream(tag, count)


def deterministic_floats(tag: str, count: int) -> np.ndarray:
    raw = _stream(tag, count * 8)
    integers = np.frombuffer(raw, dtype=">u8").astype(np.uint64)
    return (
        integers.astype(np.float64) / float(2**64) * 2.0 - 1.0
    )


def deterministic_matrix(rows: int, cols: int, tag: str, scale: float | None = None) -> np.ndarray:
    if rows <= 0 or cols <= 0:
        raise ValueError("matrix dimensions must be positive")
    values = deterministic_floats(f"matrix|{tag}|{rows}|{cols}", rows * cols)
    if scale is None:
        scale = 1.0 / np.sqrt(float(cols))
    return (values.reshape(rows, cols) * float(scale)).astype(np.float64)


def deterministic_pixels(seed: int, shape: tuple[int, int, int]) -> np.ndarray:
    height, width, channels = shape
    count = height * width * channels
    raw = _stream(f"image|seed={int(seed)}|shape={height}x{width}x{channels}", count)
    return np.frombuffer(raw, dtype=np.uint8).reshape(shape).copy()


@lru_cache(maxsize=8192)
def token_vector(lang: str, token: str, dim: int, vocab_version: int) -> tuple[float, ...]:
    values = deterministic_floats(
        f"token|lang={lang}|token={token}|dim={dim}|vocab={vocab_version}",
        dim,
    )
    norm = float(np.sqrt(np.sum(values * values)))
    if norm == 0.0:
        values = np.zeros(dim, dtype=np.float64)
        values[0] = 1.0
        norm = 1.0
    return tuple((values / norm).tolist())

