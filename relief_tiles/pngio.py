"""Small dependency-free PNG writer for grayscale slices and references."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def encode_gray_png(gray: np.ndarray) -> bytes:
    if gray.ndim != 2:
        raise ValueError("grayscale PNG input must be two-dimensional")
    if gray.dtype.kind not in "ui":
        raise ValueError("grayscale PNG input must have an integer dtype")

    height, width = gray.shape
    if gray.min(initial=0) < 0:
        raise ValueError("grayscale PNG input must be non-negative")
    maximum = int(gray.max(initial=0))
    bit_depth = 8 if maximum <= 255 else 16

    if bit_depth == 8:
        rows = (
            b"\x00" + np.ascontiguousarray(gray[row], dtype=np.uint8).tobytes()
            for row in range(height)
        )
    else:
        rows = (
            b"\x00"
            + np.ascontiguousarray(gray[row], dtype=">u2").tobytes()
            for row in range(height)
        )
    compressed = zlib.compress(b"".join(rows), level=9)
    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        0,
        0,
        0,
        0,
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def write_gray_png(path: str | Path, gray: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_gray_png(gray))
