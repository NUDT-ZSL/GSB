"""Small deterministic PNG writer and diagnostic image renderers."""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import numpy as np


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def encode_png(rgb: np.ndarray) -> bytes:
    """Encode an HxWx3 uint8 RGB array without timestamps or metadata."""

    array = np.asarray(rgb, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("RGB image must have shape HxWx3")
    height, width, _ = array.shape
    scanlines = b"".join(b"\x00" + array[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _chunk(b"IEND", b"")
    )


def write_png(path: str | Path, rgb: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encode_png(rgb))


def png_data_uri(rgb: np.ndarray) -> str:
    return "data:image/png;base64," + base64.b64encode(encode_png(rgb)).decode("ascii")


def mask_png(mask: np.ndarray) -> np.ndarray:
    value = np.where(np.asarray(mask, dtype=bool)[:, :, None], 255, 0).astype(np.uint8)
    return np.repeat(value, 3, axis=2)


def heatmap_png(values: np.ndarray) -> np.ndarray:
    """Map [0,1] values to a deterministic blue-green-yellow-red heatmap."""

    data = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    red = np.clip(3.0 * data - 1.0, 0.0, 1.0)
    green = np.clip(3.0 * data, 0.0, 1.0)
    green = np.where(data > 0.66, np.clip(3.0 - 3.0 * data, 0.0, 1.0), green)
    blue = np.clip(1.0 - 3.0 * data, 0.0, 1.0)
    rgb = np.stack((red, green, blue), axis=-1)
    return np.round(rgb * 255.0).astype(np.uint8)


def weights_rgb(weight_map: np.ndarray) -> np.ndarray:
    """Encode up to three per-view weights or aggregate weights as RGB."""

    weights = np.asarray(weight_map, dtype=np.float64)
    if weights.ndim == 2:
        return heatmap_png(weights)
    if weights.ndim != 3:
        raise ValueError("weight map must be HxW or HxWxN")
    if weights.shape[2] >= 3:
        rgb = np.clip(weights[:, :, :3], 0.0, 1.0)
    elif weights.shape[2] == 2:
        rgb = np.stack((weights[:, :, 0], weights[:, :, 1], np.zeros_like(weights[:, :, 0])), axis=-1)
    else:
        rgb = np.repeat(weights[:, :, :1], 3, axis=2)
    return np.round(rgb * 255.0).astype(np.uint8)

