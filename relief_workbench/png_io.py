from __future__ import annotations

import binascii
import os
import struct
import tempfile
import zlib
from pathlib import Path

import numpy as np

from .errors import WorkbenchError

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_gray_png(image: np.ndarray) -> bytes:
    array = np.asarray(image)
    if array.ndim != 2 or array.dtype != np.uint8:
        raise ValueError("PNG encoder expects a 2-D uint8 array")
    height, width = array.shape
    scanlines = b"".join(b"\x00" + row.tobytes() for row in array)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + _chunk(b"IEND", b"")
    )


def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def write_gray_png(path: str | Path, image: np.ndarray) -> Path:
    return atomic_write_bytes(path, encode_gray_png(image))


def write_png_if_requested(path: str | Path, image: np.ndarray) -> Path:
    try:
        return write_gray_png(path, image)
    except OSError as exc:
        raise WorkbenchError(
            "E_OUTPUT_WRITE",
            "unable to write PNG output",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
