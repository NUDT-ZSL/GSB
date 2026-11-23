from __future__ import annotations

import struct
import zlib

import numpy as np


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_rgba_png(rgba: np.ndarray) -> bytes:
    array = np.ascontiguousarray(rgba)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 4:
        raise ValueError("RGBA PNG input must be HxWx4 uint8")
    height, width, _ = array.shape
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level=6))
        + _chunk(b"IEND", b"")
    )

