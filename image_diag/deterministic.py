from __future__ import annotations

import hashlib
import json
import struct
import zlib
from typing import Any, Mapping

import numpy as np


_MASK64 = (1 << 64) - 1


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _normalized_array(array: np.ndarray) -> tuple[str, tuple[int, ...], bytes]:
    arr = np.asarray(array)
    if arr.dtype.kind == "f":
        dtype = np.dtype("<f8")
        arr = arr.astype(dtype, copy=False)
    elif arr.dtype.kind in "iu":
        dtype = np.dtype("<i8")
        arr = arr.astype(dtype, copy=False)
    elif arr.dtype.kind == "b":
        dtype = np.dtype("u1")
        arr = arr.astype(dtype, copy=False)
    else:
        dtype = arr.dtype
    contiguous = np.ascontiguousarray(arr)
    return dtype.str, tuple(int(x) for x in contiguous.shape), contiguous.tobytes()


def array_digest(array: np.ndarray) -> str:
    dtype, shape, payload = _normalized_array(array)
    h = hashlib.sha256()
    h.update(dtype.encode("ascii"))
    h.update(struct.pack("<I", len(shape)))
    for dimension in shape:
        h.update(struct.pack("<Q", dimension))
    h.update(payload)
    return h.hexdigest()


def combine_digests(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        h.update(struct.pack("<I", len(encoded)))
        h.update(encoded)
    return h.hexdigest()


def _splitmix64(values: np.ndarray) -> np.ndarray:
    z = values.astype(np.uint64, copy=True)
    with np.errstate(over="ignore"):
        z = z + np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    return z


def deterministic_noise(
    seed: int,
    shape: tuple[int, int],
    *,
    salt: int = 0,
    region_labels: np.ndarray | None = None,
) -> np.ndarray:
    """Return uint64-addressed values in [-0.5, 0.5), independent of RNG state."""

    height, width = shape
    yy, xx = np.mgrid[0:height, 0:width]
    index = yy.astype(np.uint64) * np.uint64(width) + xx.astype(np.uint64)
    key = np.uint64(seed & _MASK64) ^ np.uint64(salt & _MASK64)
    values = index * np.uint64(0xD6E8FEB86659FD93) ^ key
    if region_labels is not None:
        labels = np.asarray(region_labels, dtype=np.uint64)
        values = values ^ labels * np.uint64(0xA24BAED4963EE407)
    mixed = _splitmix64(values)
    unit = (mixed >> np.uint64(11)).astype(np.float64) / float(1 << 53)
    return unit - 0.5


def protect_subnormal(
    array: np.ndarray,
    *,
    floor: float | None = None,
) -> np.ndarray:
    values = np.asarray(array, dtype=np.float64)
    threshold = np.finfo(np.float64).tiny if floor is None else float(floor)
    return np.where(np.abs(values) < threshold, 0.0, values)


def stable_ge(
    values: np.ndarray,
    threshold: float,
    *,
    policy: str,
    tolerance: float,
    protect: bool = True,
) -> np.ndarray:
    """Precision-independent threshold comparison with an optional stable band."""

    control = np.asarray(values, dtype=np.float64)
    if protect:
        control = protect_subnormal(control)
    if policy == "strict":
        return control >= float(threshold)
    if policy == "tolerance_band":
        return control >= float(threshold) - float(tolerance)
    raise ValueError(f"unknown threshold policy: {policy}")


def png_encode(rgb: np.ndarray) -> bytes:
    """Encode an HxWx3 uint8 image as a deterministic PNG without Pillow."""

    image = np.asarray(rgb)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("png_encode expects HxWx3 uint8")
    height, width, _ = image.shape

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanlines = b"".join(b"\x00" + image[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def to_uint8(image: np.ndarray) -> np.ndarray:
    values = np.clip(np.asarray(image, dtype=np.float64), 0.0, 1.0)
    return np.rint(values * 255.0).astype(np.uint8)


def diff_heatmap(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    delta = np.abs(np.asarray(left, dtype=np.float64) - np.asarray(right, dtype=np.float64))
    if delta.ndim == 3:
        delta = np.max(delta, axis=2)
    scale = float(delta.max())
    normalized = delta / scale if scale > 0.0 else np.zeros_like(delta)
    # Dark blue -> cyan -> yellow -> white, deterministic and dependency-free.
    r = np.clip(1.7 * normalized - 0.35, 0.0, 1.0)
    g = np.clip(1.9 * normalized - 0.10, 0.0, 1.0)
    b = np.clip(1.4 * (1.0 - normalized) + 0.25 * normalized, 0.0, 1.0)
    return to_uint8(np.stack([r, g, b], axis=-1))


def summarize_array(array: np.ndarray) -> Mapping[str, Any]:
    arr = np.asarray(array, dtype=np.float64)
    return {
        "shape": [int(x) for x in arr.shape],
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "mean": float(np.mean(arr)),
    }
