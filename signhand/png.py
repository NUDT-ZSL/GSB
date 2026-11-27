"""Dependency-free deterministic PNG writer for RGB uint8 images."""

from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path

import numpy as np


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


def png_bytes(image: np.ndarray, compression_level: int = 9) -> bytes:
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("PNG image must have shape (height, width, 3)")
    image = np.ascontiguousarray(np.clip(image, 0, 255).astype(np.uint8))
    height, width, _ = image.shape
    scanlines = np.concatenate(
        [np.zeros((height, 1), dtype=np.uint8), image.reshape(height, width * 3)],
        axis=1,
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(scanlines.tobytes(), level=compression_level))
        + _chunk(b"IEND", b"")
    )


def write_png(path: str | Path, image: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(image))

