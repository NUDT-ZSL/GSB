"""Minimal dependency-free PNG encoding for coverage visualisations."""
from __future__ import annotations

import struct
import zlib
from typing import Iterable, Tuple

import numpy as np


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(rgb: np.ndarray) -> bytes:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("expected an HxWx3 uint8 array")
    height, width, _ = rgb.shape
    raw = bytearray()
    for row in rgb.astype(np.uint8):
        raw.append(0)
        raw.extend(row.tobytes())
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (header + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def _viridis_lut() -> np.ndarray:
    stops = np.array([
        [0.02, 0.01, 0.10],
        [0.20, 0.15, 0.45],
        [0.15, 0.45, 0.55],
        [0.10, 0.65, 0.45],
        [0.55, 0.82, 0.25],
        [0.98, 0.92, 0.35],
    ])
    x = np.linspace(0.0, 1.0, 256)
    xp = np.linspace(0.0, 1.0, len(stops))
    lut = np.empty((256, 3), dtype=np.float64)
    for channel in range(3):
        lut[:, channel] = np.interp(x, xp, stops[:, channel])
    return (lut * 255.0).astype(np.uint8)


LUT = _viridis_lut()


def coverage_to_rgb(coverage: np.ndarray, background: Tuple[int, int, int] = (12, 14, 22)
                    ) -> np.ndarray:
    height, width = coverage.shape
    scaled = np.clip(coverage, 0.0, 1.0)
    indices = np.round(scaled * 255.0).astype(np.uint8)
    colored = LUT[indices]
    bg = np.asarray(background, dtype=np.float64)
    blend = (colored.astype(np.float64) * scaled[..., None]
             + bg * (1.0 - scaled[..., None]))
    return np.clip(blend, 0, 255).astype(np.uint8).reshape(height, width, 3)


def overlay_pixels(rgb: np.ndarray, pixels: Iterable[Tuple[int, int]],
                   color: Tuple[int, int, int] = (255, 72, 72)) -> np.ndarray:
    out = rgb.copy()
    height, width = out.shape[:2]
    for x, y in pixels:
        if 0 <= x < width and 0 <= y < height:
            out[y, x] = np.asarray(color, dtype=np.uint8)
    return out


def side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    gap = np.full((max(left.shape[0], right.shape[0]), 8, 3), 24, dtype=np.uint8)
    if left.shape[0] != right.shape[0]:
        target = max(left.shape[0], right.shape[0])
        left = _pad_height(left, target)
        right = _pad_height(right, target)
    return np.concatenate([left, gap, right], axis=1)


def _pad_height(rgb: np.ndarray, target: int) -> np.ndarray:
    pad = np.zeros((target - rgb.shape[0], rgb.shape[1], 3), dtype=np.uint8)
    return np.concatenate([rgb, pad], axis=0)


def upscale_rgb(rgb: np.ndarray, factor: int) -> np.ndarray:
    return np.repeat(np.repeat(rgb, factor, axis=0), factor, axis=1)
