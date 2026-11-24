from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Dict, Tuple


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_gray_png(gray, metadata: Dict[str, str] | None = None) -> bytes:
    """Encode a uint8 or uint16 single-channel image without Pillow."""

    import numpy as np

    array = np.ascontiguousarray(gray)
    if array.ndim != 2:
        raise ValueError("grayscale PNG must be two-dimensional")
    height, width = array.shape
    if array.dtype == np.uint8:
        bit_depth = 8
        raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    elif array.dtype == np.uint16:
        bit_depth = 16
        array = array.astype(">u2", copy=False)
        raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    else:
        raise TypeError("grayscale PNG requires uint8 or uint16 data")

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 0, 0, 0, 0)
    result = bytearray(_PNG_SIGNATURE)
    result.extend(_chunk(b"IHDR", ihdr))
    for key, value in (metadata or {}).items():
        key_bytes = key.encode("latin-1", "replace")[:79]
        value_bytes = str(value).encode("latin-1", "replace")
        result.extend(_chunk(b"tEXt", key_bytes + b"\x00" + value_bytes))
    result.extend(_chunk(b"IDAT", zlib.compress(raw, 6)))
    result.extend(_chunk(b"IEND", b""))
    return bytes(result)


def read_png_dimensions(path: str | Path) -> Tuple[int, int]:
    with open(path, "rb") as handle:
        signature = handle.read(8)
        if signature != _PNG_SIGNATURE:
            raise ValueError("not a PNG file")
        length = struct.unpack(">I", handle.read(4))[0]
        tag = handle.read(4)
        if tag != b"IHDR" or length < 8:
            raise ValueError("missing PNG IHDR")
        width, height = struct.unpack(">II", handle.read(8))
    return width, height


def read_png_text(path: str | Path) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    data = Path(path).read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        return texts
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if tag == b"tEXt" and b"\x00" in body:
            key, value = body.split(b"\x00", 1)
            texts[key.decode("latin-1")] = value.decode("latin-1", "replace")
        offset += 12 + length
        if tag == b"IEND":
            break
    return texts
