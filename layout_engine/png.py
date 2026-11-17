from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import ConfigError, ImageFormatError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_MODES = {
    0: ("L", 1),
    2: ("RGB", 3),
    4: ("LA", 2),
    6: ("RGBA", 4),
}


@dataclass(frozen=True)
class PNGInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int
    pixel_mode: str
    channels: int


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


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    stride = width * bpp
    expected = (stride + 1) * height
    if len(raw) != expected:
        raise ValueError(f"decompressed size {len(raw)} != expected {expected}")
    output = bytearray(stride * height)
    previous = bytearray(stride)
    source_pos = 0
    target_pos = 0
    for _ in range(height):
        filter_type = raw[source_pos]
        source_pos += 1
        row = raw[source_pos : source_pos + stride]
        source_pos += stride
        reconstructed = bytearray(stride)
        if filter_type == 0:
            reconstructed[:] = row
        elif filter_type == 1:
            for i, value in enumerate(row):
                left = reconstructed[i - bpp] if i >= bpp else 0
                reconstructed[i] = (value + left) & 0xFF
        elif filter_type == 2:
            for i, value in enumerate(row):
                reconstructed[i] = (value + previous[i]) & 0xFF
        elif filter_type == 3:
            for i, value in enumerate(row):
                left = reconstructed[i - bpp] if i >= bpp else 0
                up = previous[i]
                reconstructed[i] = (value + ((left + up) >> 1)) & 0xFF
        elif filter_type == 4:
            for i, value in enumerate(row):
                left = reconstructed[i - bpp] if i >= bpp else 0
                up = previous[i]
                upper_left = previous[i - bpp] if i >= bpp else 0
                reconstructed[i] = (value + _paeth(left, up, upper_left)) & 0xFF
        else:
            raise ValueError(f"unsupported PNG filter type {filter_type}")
        output[target_pos : target_pos + stride] = reconstructed
        target_pos += stride
        previous = reconstructed
    return output


def load_png_gray_bytes(
    data: bytes,
    *,
    page: str,
    expected_width: int,
    expected_height: int,
) -> tuple[np.ndarray, PNGInfo]:
    if not data.startswith(PNG_SIGNATURE):
        raise ImageFormatError("invalid PNG signature", page=page)
    position = len(PNG_SIGNATURE)
    info: PNGInfo | None = None
    idat_parts: list[bytes] = []
    saw_iend = False
    chunk_index = 0
    try:
        while position < len(data):
            if position + 8 > len(data):
                raise ValueError("truncated chunk header")
            length = struct.unpack(">I", data[position : position + 4])[0]
            chunk_type = data[position + 4 : position + 8]
            position += 8
            if position + length + 4 > len(data):
                raise ValueError("truncated chunk payload")
            chunk_data = data[position : position + length]
            stored_crc = struct.unpack(
                ">I", data[position + length : position + length + 4]
            )[0]
            computed_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            if stored_crc != computed_crc:
                raise ValueError(f"CRC mismatch in {chunk_type.decode('latin1', 'replace')}")
            position += length + 4
            if chunk_index == 0 and chunk_type != b"IHDR":
                raise ValueError("IHDR is not the first chunk")
            if chunk_type == b"IHDR":
                if info is not None or length != 13:
                    raise ValueError("invalid or duplicate IHDR")
                (
                    width,
                    height,
                    bit_depth,
                    color_type,
                    compression,
                    filter_method,
                    interlace,
                ) = struct.unpack(">IIBBBBB", chunk_data)
                if width <= 0 or height <= 0:
                    raise ValueError("PNG dimensions must be positive")
                if compression != 0 or filter_method != 0 or interlace != 0:
                    raise ValueError("unsupported PNG compression/filter/interlace mode")
                if bit_depth != 8:
                    raise ValueError(f"unsupported bit depth {bit_depth}; only 8-bit is supported")
                if color_type not in COLOR_MODES:
                    raise ValueError(f"unsupported pixel mode/color type {color_type}")
                mode, channels = COLOR_MODES[color_type]
                info = PNGInfo(width, height, bit_depth, color_type, mode, channels)
            elif chunk_type == b"IDAT":
                idat_parts.append(chunk_data)
            elif chunk_type == b"IEND":
                if length != 0:
                    raise ValueError("IEND must be empty")
                saw_iend = True
                break
            elif chunk_type and chunk_type[0] & 0x20 == 0:
                raise ValueError(
                    f"unsupported critical chunk {chunk_type.decode('latin1', 'replace')}"
                )
            chunk_index += 1
        if info is None:
            raise ValueError("missing IHDR")
        if not idat_parts:
            raise ValueError("missing IDAT")
        if not saw_iend:
            raise ValueError("missing IEND")
        if info.width != expected_width or info.height != expected_height:
            raise ValueError(
                f"dimension mismatch: got {info.width}x{info.height}, "
                f"expected {expected_width}x{expected_height}"
            )
        decompressed = zlib.decompress(b"".join(idat_parts))
        raw = _unfilter(decompressed, info.width, info.height, info.channels)
    except ImageFormatError:
        raise
    except (ValueError, zlib.error, struct.error) as exc:
        raise ImageFormatError(str(exc), page=page) from exc

    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(info.height, info.width, info.channels)
    if info.color_type == 0:
        gray = pixels[:, :, 0]
    elif info.color_type == 4:
        gray = pixels[:, :, 0]
    elif info.color_type == 2:
        rgb = pixels.astype(np.uint32)
        gray = ((299 * rgb[:, :, 0] + 587 * rgb[:, :, 1] + 114 * rgb[:, :, 2]) // 1000).astype(
            np.uint8
        )
    else:
        rgb = pixels[:, :, :3].astype(np.uint32)
        gray = ((299 * rgb[:, :, 0] + 587 * rgb[:, :, 1] + 114 * rgb[:, :, 2]) // 1000).astype(
            np.uint8
        )
    return np.ascontiguousarray(gray), info


def load_png_gray(
    path: str | Path,
    *,
    page: str,
    expected_width: int,
    expected_height: int,
) -> tuple[np.ndarray, PNGInfo, bytes]:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ImageFormatError(f"cannot read image: {exc}", page=page) from exc
    gray, info = load_png_gray_bytes(
        data,
        page=page,
        expected_width=expected_width,
        expected_height=expected_height,
    )
    return gray, info, data


def validate_metadata_dimensions(metadata: dict[str, Any]) -> tuple[int, int]:
    try:
        width = int(metadata["page_width"])
        height = int(metadata["page_height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("metadata must define integer page_width and page_height") from exc
    if width <= 0 or height <= 0:
        raise ConfigError("metadata page dimensions must be positive")
    return width, height
