from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from .errors import AuditError, ErrorCode


def write_rgb_png(path: str | Path, rgb: np.ndarray) -> None:
    array = np.asarray(rgb, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("rgb must have shape (height, width, 3)")
    height, width, _ = array.shape

    raw = bytearray()
    for row in array:
        raw.append(0)
        raw.extend(row.tobytes())

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def read_rgb_png(path: str | Path) -> np.ndarray:
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "invalid PNG signature", {"path": str(path)})

    offset = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif tag == b"IDAT":
            idat.extend(chunk_data)
        elif tag == b"IEND":
            break

    if bit_depth != 8 or color_type != 2 or not width or not height:
        raise AuditError(
            ErrorCode.FORMAT_UNSUPPORTED,
            "only 8-bit RGB PNG is supported",
            {"bit_depth": bit_depth, "color_type": color_type},
        )

    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            "PNG raw data length mismatch",
            {"expected": expected, "actual": len(raw)},
        )

    output = np.zeros((height, width, 3), dtype=np.uint8)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = np.frombuffer(raw[pos : pos + stride], dtype=np.uint8).copy()
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for x in range(3, stride):
                line[x] = (int(line[x]) + int(line[x - 3])) & 0xFF
        elif filter_type == 2:
            if y:
                prev = output[y - 1].reshape(-1)
                line = (line.astype(np.uint16) + prev.astype(np.uint16)).astype(np.uint8)
        elif filter_type == 3:
            prev = output[y - 1].reshape(-1) if y else np.zeros(stride, dtype=np.uint8)
            for x in range(stride):
                left = line[x - 3] if x >= 3 else 0
                up = prev[x]
                line[x] = (int(line[x]) + (int(left) + int(up)) // 2) & 0xFF
        elif filter_type == 4:
            prev = output[y - 1].reshape(-1) if y else np.zeros(stride, dtype=np.uint8)
            for x in range(stride):
                a = line[x - 3] if x >= 3 else 0
                b = prev[x]
                c = prev[x - 3] if x >= 3 else 0
                p = int(a) + int(b) - int(c)
                pa, pb, pc = abs(p - int(a)), abs(p - int(b)), abs(p - int(c))
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (int(line[x]) + int(predictor)) & 0xFF
        else:
            raise AuditError(
                ErrorCode.FORMAT_UNSUPPORTED,
                "unsupported PNG filter",
                {"filter_type": filter_type, "row": y},
            )
        output[y] = line.reshape(width, 3)
    return output


def png_size(path: str | Path) -> tuple[int, int]:
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "invalid PNG signature", {"path": str(path)})
    width, height = struct.unpack(">II", data[16:24])
    return height, width
