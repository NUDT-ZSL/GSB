from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from .storage import atomic_write_bytes


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def rgba_to_png_bytes(rgba: np.ndarray) -> bytes:
    array = np.asarray(rgba, dtype=np.float64)
    if array.ndim != 3 or array.shape[2] != 4:
        raise ValueError("RGBA image must have shape (height, width, 4)")
    height, width, _ = array.shape
    pixels = np.clip(np.rint(array * 255.0), 0.0, 255.0).astype(np.uint8)
    raw = b"".join(b"\x00" + pixels[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level=9))
        + _chunk(b"IEND", b"")
    )


def write_rgba_png(path: str | Path, rgba: np.ndarray) -> None:
    atomic_write_bytes(path, rgba_to_png_bytes(rgba))
