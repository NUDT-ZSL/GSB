"""Minimal dependency-free PNG output and inspection."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

from .errors import error


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_grayscale_png(rows: list[list[int]], width: int, height: int) -> bytes:
    if len(rows) != height or any(len(row) != width for row in rows):
        raise error("E_OUTPUT", "grayscale image dimensions are inconsistent")
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for value in row:
            raw.append(max(0, min(255, int(value))))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def read_png_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE) or len(data) < 24:
        raise error("E_DIGEST", "not a valid PNG file")
    length = struct.unpack(">I", data[8:12])[0]
    kind = data[12:16]
    if kind != b"IHDR" or length != 13:
        raise error("E_DIGEST", "PNG is missing a canonical IHDR chunk")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def read_png_dimensions_from_path(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as handle:
            header = handle.read(33)
    except OSError:
        return None
    try:
        return read_png_dimensions(header)
    except Exception:
        return None

