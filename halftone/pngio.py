from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Tuple

import numpy as np

from .errors import BusinessError

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, channels: int, bit_depth: int) -> bytes:
    if bit_depth != 8:
        raise BusinessError(
            "E_SOURCE",
            "sample PNG reader currently supports 8-bit grayscale/RGBA images",
            details={"bit_depth": bit_depth},
        )
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise BusinessError(
            "E_SOURCE",
            "PNG IDAT length does not match image dimensions",
            details={"expected": expected, "actual": len(raw)},
        )

    output = bytearray(height * stride)
    previous = bytearray(stride)
    source_pos = 0
    target_pos = 0
    for _ in range(height):
        filter_type = raw[source_pos]
        source_pos += 1
        row = bytearray(raw[source_pos : source_pos + stride])
        source_pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + previous[i]) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up_left = previous[i - channels] if i >= channels else 0
                row[i] = (
                    row[i] + _paeth(left, previous[i], up_left)
                ) & 0xFF
        else:
            raise BusinessError(
                "E_SOURCE", "unsupported PNG filter", {"filter_type": filter_type}
            )
        output[target_pos : target_pos + stride] = row
        target_pos += stride
        previous = row
    return bytes(output)


def read_png(path: str | Path) -> np.ndarray:
    """Read an 8-bit PNG and return a 2-D uint8 grayscale array."""

    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise BusinessError("E_SOURCE", f"cannot read image: {file_path}") from exc

    if not data.startswith(_PNG_SIGNATURE):
        raise BusinessError("E_SOURCE", f"not a PNG file: {file_path}")
    pos = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length, chunk_type = struct.unpack(">I4s", data[pos : pos + 8])
        pos += 8
        chunk_data = data[pos : pos + length]
        pos += length + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width is None or height is None or color_type is None:
        raise BusinessError("E_SOURCE", "PNG is missing IHDR")
    if interlace != 0:
        raise BusinessError("E_SOURCE", "interlaced PNG is not supported")
    if bit_depth != 8 or color_type not in (0, 2, 4, 6):
        raise BusinessError(
            "E_SOURCE",
            "only 8-bit gray, RGB, gray-alpha, and RGBA PNG inputs are supported",
            details={"bit_depth": bit_depth, "color_type": color_type},
        )

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    try:
        raw = zlib.decompress(bytes(idat))
        pixels = _unfilter(raw, width, height, channels, bit_depth or 8)
    except zlib.error as exc:
        raise BusinessError("E_SOURCE", "cannot decompress PNG IDAT") from exc

    array = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, channels)
    if color_type == 0:
        return np.ascontiguousarray(array[:, :, 0])
    if color_type == 4:
        gray = array[:, :, 0].astype(np.float64)
        alpha = array[:, :, 1].astype(np.float64) / 255.0
        composed = 255.0 * (1.0 - alpha) + gray * alpha
        return np.rint(composed).astype(np.uint8)

    rgb = array[:, :, :3].astype(np.float64)
    gray = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    if color_type == 6:
        alpha = array[:, :, 3].astype(np.float64) / 255.0
        gray = 255.0 * (1.0 - alpha) + gray * alpha
    return np.rint(gray).astype(np.uint8)


def png_bytes(gray: np.ndarray) -> bytes:
    array = np.asarray(gray)
    if array.dtype != np.uint8 or array.ndim != 2:
        raise ValueError("png_bytes expects a 2-D uint8 array")
    height, width = array.shape
    raw = b"".join(b"\x00" + array[y, :].tobytes() for y in range(height))

    def chunk(chunk_type: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(raw, 6)
    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def write_png(path: str | Path, gray: np.ndarray) -> Path:
    from .atomicio import atomic_write_bytes

    return atomic_write_bytes(path, png_bytes(gray))


def write_rgb_png(path: str | Path, rgb: np.ndarray) -> Path:
    """Write an 8-bit RGB PNG; used only for ink-tone previews."""

    array = np.asarray(rgb, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("write_rgb_png expects an HxWx3 uint8 array")
    height, width, _ = array.shape
    raw = b"".join(b"\x00" + array[y, :, :].tobytes() for y in range(height))

    def chunk(chunk_type: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, 6)
    data = (
        _PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )
    from .atomicio import atomic_write_bytes

    return atomic_write_bytes(path, data)
