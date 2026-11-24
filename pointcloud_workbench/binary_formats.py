"""PNG/PFM image codecs used by final outputs and tests."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from .errors import BusinessError
from .util import atomic_write_bytes


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(array: np.ndarray, mode: str = "gray") -> bytes:
    """Encode an 8-bit grayscale or RGB array without external libraries."""

    if mode == "gray":
        if array.ndim != 2 or array.dtype != np.uint8:
            raise ValueError("grayscale PNG requires a 2-D uint8 array")
        color_type = 0
        height, width = array.shape
        rows = array
    elif mode == "rgb":
        if array.ndim != 3 or array.shape[2] != 3 or array.dtype != np.uint8:
            raise ValueError("RGB PNG requires an HxWx3 uint8 array")
        color_type = 2
        height, width, _ = array.shape
        rows = array
    else:
        raise ValueError(f"unsupported PNG mode: {mode}")

    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row.tobytes(order="C"))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def decode_png_gray(data: bytes) -> np.ndarray:
    """Decode the filter-0 8-bit grayscale PNGs produced by this project."""

    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    offset = len(PNG_SIGNATURE)
    width = height = None
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", payload[:10])
            if depth != 8 or color_type != 0:
                raise ValueError("only 8-bit grayscale PNG is supported")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    if width is None or height is None:
        raise ValueError("PNG has no IHDR")
    raw = zlib.decompress(bytes(compressed))
    stride = width + 1
    result = np.empty((height, width), dtype=np.uint8)
    for row_index in range(height):
        row = raw[row_index * stride : (row_index + 1) * stride]
        if row[0] != 0:
            raise ValueError("only PNG filter 0 is supported")
        result[row_index] = np.frombuffer(row[1:], dtype=np.uint8)
    return result


def write_png_atomic(path: Path, array: np.ndarray, mode: str = "gray") -> None:
    atomic_write_bytes(Path(path), encode_png(array, mode=mode))


def encode_pfm(array: np.ndarray) -> bytes:
    """Encode a float32 depth map in world units using PFM."""

    if array.ndim != 2:
        raise ValueError("PFM requires a 2-D array")
    height, width = array.shape
    header = f"PF\n{width} {height}\n-1.0\n".encode("ascii")
    payload = np.asarray(array, dtype="<f4")[::-1, :].tobytes(order="C")
    return header + payload


def decode_pfm(data: bytes) -> np.ndarray:
    if not data.startswith(b"PF\n"):
        raise ValueError("not a little-endian PFM")
    parts = data.split(b"\n", 3)
    if len(parts) != 4:
        raise ValueError("truncated PFM header")
    _, dims, scale, payload = parts
    width_text, height_text = dims.split()
    width = int(width_text)
    height = int(height_text)
    expected = width * height * 4
    if len(payload) != expected or float(scale) >= 0:
        raise ValueError("unsupported or truncated PFM")
    array = np.frombuffer(payload, dtype="<f4").reshape(height, width)[::-1, :].copy()
    return array


def read_png_gray(path: Path) -> np.ndarray:
    try:
        return decode_png_gray(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise BusinessError(
            "E_OUTPUT",
            "cannot decode grayscale PNG",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc


def read_pfm(path: Path) -> np.ndarray:
    try:
        return decode_pfm(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise BusinessError(
            "E_OUTPUT",
            "cannot decode depth PFM",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc


def depth_preview(depth: np.ndarray) -> np.ndarray:
    """Normalize finite world-depth samples into an 8-bit preview."""

    finite = np.isfinite(depth)
    result = np.zeros(depth.shape, dtype=np.uint8)
    if not finite.any():
        return result
    values = depth[finite].astype(np.float64)
    near = float(values.min())
    far = float(values.max())
    if far <= near:
        result[finite] = 255
        return result
    normalized = (depth[finite].astype(np.float64) - near) / (far - near)
    inverted = 1.0 - normalized
    result[finite] = np.floor(inverted * 255.0 + 0.5).astype(np.uint8)
    return result

