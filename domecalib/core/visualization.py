"""Dependency-free image encoding (binary PPM/PGM) for CLI and Tk preview."""

from __future__ import annotations

import numpy as np


def _normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=float)
    finite = matrix[np.isfinite(matrix)]
    if finite.size == 0:
        return np.zeros_like(matrix, dtype=np.uint8)
    lo, hi = float(finite.min()), float(finite.max())
    if hi - lo < 1e-15:
        scaled = np.where(np.isfinite(matrix), 128.0, 0.0)
    else:
        scaled = (matrix - lo) / (hi - lo) * 255.0
    return np.nan_to_num(scaled, nan=0.0).astype(np.uint8)


def encode_pgm(matrix: np.ndarray) -> bytes:
    """Encode a 2D array as binary grayscale PGM (P5) bytes."""
    gray = _normalize(matrix)
    h, w = gray.shape
    header = f"P5\n{w} {h}\n255\n".encode("ascii")
    return header + gray.astype(np.uint8).tobytes()


def encode_ppm(rgb: np.ndarray) -> bytes:
    """Encode an (H, W, 3) float array in [0, 1] as binary PPM (P6)."""
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    h, w, _ = rgb.shape
    uint8 = (rgb * 255.0 + 0.5).astype(np.uint8)
    header = f"P6\n{w} {h}\n255\n".encode("ascii")
    return header + uint8.tobytes()


def decode_ppm(data: bytes) -> np.ndarray:
    """Decode binary PPM/PGM bytes back to a numpy array (used by the GUI)."""
    tokens = []
    idx = 0
    # Parse exactly four whitespace separated header tokens.
    while len(tokens) < 4:
        nl = data.find(b"\n", idx)
        line = data[idx:nl].strip()
        idx = nl + 1
        if not line or line.startswith(b"#"):
            continue
        tokens.extend(line.split())
    magic = tokens[0]
    width, height = int(tokens[1]), int(tokens[2])
    payload = data[idx:]
    if magic == b"P5":
        return np.frombuffer(payload, dtype=np.uint8, count=width * height).reshape(height, width)
    return np.frombuffer(
        payload, dtype=np.uint8, count=width * height * 3
    ).reshape(height, width, 3)


def lut_visualization(lut) -> np.ndarray:
    """False-colour RGB image of a LUT: red=longitude, green=latitude, mask."""
    h, w = lut.lon.shape
    lon_img = _normalize(np.where(lut.valid, lut.lon, np.nan)).astype(float) / 255.0
    lat_img = _normalize(np.where(lut.valid, lut.lat, np.nan)).astype(float) / 255.0
    valid = lut.valid.astype(float)
    rgb = np.stack([lon_img, lat_img, valid], axis=-1)
    return rgb


def mask_visualization(mask) -> np.ndarray:
    rgb = np.zeros((mask.height, mask.width, 3), dtype=float)
    w = np.clip(mask.weight, 0.0, 1.0)
    rgb[..., 0] = w
    rgb[..., 1] = w
    rgb[..., 2] = np.where(mask.valid, w, 0.0)
    return rgb
