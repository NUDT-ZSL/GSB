"""Deterministic synthetic meeting-frame decoding and tile feature extraction."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

import numpy as np

from .canonical import digest, f64_b64, float_text, sha256_hex
from .errors import BusinessError, E_RESOURCE_LIMIT
from .models import DECODER_VERSION, TEXT_EMBEDDING_DIM, VISUAL_VERSION, FeatureConfig


def validate_pixel_budget(shape: Tuple[int, int, int], max_bytes: int, pair_id: str | None = None) -> int:
    height, width, channels = shape
    size = int(height) * int(width) * int(channels)
    if size > max_bytes:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "单画面驻留像素内存超过配置上限",
            pair_id=pair_id,
            field="image_shape",
            details={"required_bytes": size, "max_pixel_bytes_per_image": max_bytes},
        )
    return size


def _gradient(height: int, width: int, denominator_height: int, denominator_width: int) -> np.ndarray:
    y = np.arange(height, dtype=np.int64)[:, None]
    x = np.arange(width, dtype=np.int64)[None, :]
    if denominator_height > 0:
        y = y * 255 // denominator_height
    else:
        y = np.zeros_like(y)
    if denominator_width > 0:
        x = x * 255 // denominator_width
    else:
        x = np.zeros_like(x)
    return np.broadcast_to((x + y) % 256, (height, width)).astype(np.uint8)


def _fill_texture(
    canvas: np.ndarray,
    y0: int,
    y1: int,
    x0: int,
    x1: int,
    color: Tuple[int, int, int],
    pattern: int,
    texture_cell: int,
) -> None:
    height = y1 - y0
    width = x1 - x0
    local_y = np.arange(height, dtype=np.int64)[:, None]
    local_x = np.arange(width, dtype=np.int64)[None, :]
    base = np.asarray(color, dtype=np.int16).reshape(1, 1, 3)
    texture = np.zeros((height, width, 1), dtype=np.int16)
    if pattern == 0:
        texture.fill(0)
    elif pattern == 1:
        texture = (((local_y // texture_cell) + (local_x // texture_cell)) % 2 * 24).astype(np.int16)[:, :, None]
    elif pattern == 2:
        texture = ((local_y // texture_cell) % 2 * 20).astype(np.int16)[:, :, None]
    elif pattern == 3:
        texture = ((local_x // texture_cell) % 2 * 20).astype(np.int16)[:, :, None]
    else:
        texture = (((local_y + local_x) % 16) * 2).astype(np.int16)[:, :, None]
    block = np.clip(base + texture, 0, 255).astype(np.uint8)
    canvas[y0:y1, x0:x1, :] = block


def decode_frame(image_seed: int, image_shape: Tuple[int, int, int], slide_rect: Tuple[int, int, int, int], texture_cell: int) -> np.ndarray:
    """Decode a seeded RGB frame using integer-only deterministic drawing."""
    height, width, channels = image_shape
    if channels != 3:
        raise BusinessError(E_RESOURCE_LIMIT, "仅支持 RGB 三通道画面", field="image_shape")
    rng = np.random.Generator(np.random.PCG64(int(image_seed)))

    canvas = np.empty((height, width, 3), dtype=np.uint8)
    canvas[:, :, 0] = _gradient(height, width, max(height - 1, 0), max(width - 1, 0))
    canvas[:, :, 1] = _gradient(height, width, max(width - 1, 0), max(height - 1, 0))
    canvas[:, :, 2] = (canvas[:, :, 0] // 3 + canvas[:, :, 1] // 2) % 256
    noise = rng.integers(0, 32, size=(height, width, 1), dtype=np.uint8)
    canvas = np.clip(canvas.astype(np.int16) + noise.astype(np.int16), 0, 255).astype(np.uint8)

    x0, y0, x1, y1 = slide_rect
    slide_height = y1 - y0
    slide_width = x1 - x0
    slide_base = tuple(int(v) for v in rng.integers(24, 224, size=3, dtype=np.int64))
    _fill_texture(canvas, y0, y1, x0, x1, slide_base, 0, texture_cell)

    block_count = 4
    boundaries_y = [y0 + (slide_height * index) // block_count for index in range(block_count + 1)]
    boundaries_x = [x0 + (slide_width * index) // block_count for index in range(block_count + 1)]
    for block_index in range(block_count):
        by0 = boundaries_y[block_index]
        by1 = boundaries_y[block_index + 1]
        bx0 = boundaries_x[block_index]
        bx1 = boundaries_x[block_index + 1]
        color = tuple(int(v) for v in rng.integers(16, 240, size=3, dtype=np.int64))
        pattern = int(rng.integers(0, 5))
        _fill_texture(canvas, by0, by1, bx0, bx1, color, pattern, texture_cell)
    return np.ascontiguousarray(canvas, dtype=np.uint8)


def frame_hash(canvas: np.ndarray) -> str:
    return sha256_hex(canvas.tobytes(order="C"))


def _tile_boundaries(start: int, end: int, parts: int) -> List[Tuple[int, int]]:
    return [(start + (end - start) * index // parts, start + (end - start) * (index + 1) // parts) for index in range(parts)]


def _projection_matrix(feature_config: FeatureConfig, raw_dim: int) -> np.ndarray:
    rows = raw_dim
    cols = TEXT_EMBEDDING_DIM
    matrix = np.empty((rows, cols), dtype=np.float64)
    prefix = f"{feature_config.projection_seed}|{raw_dim}|{cols}|".encode("utf-8")
    for row in range(rows):
        for col in range(cols):
            value = hashlib.blake2b(prefix + f"{row}:{col}".encode("ascii"), digest_size=8).digest()
            integer = int.from_bytes(value, "little", signed=False)
            matrix[row, col] = (integer / float(2**63)) - 1.0
    return matrix


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return np.zeros_like(vector, dtype=np.float64)
    return (vector / norm).astype(np.float64)


def extract_visual_features(
    canvas: np.ndarray,
    slide_rect: Tuple[int, int, int, int],
    feature_config: FeatureConfig,
) -> Dict[str, Any]:
    feature_config.validate()
    x0, y0, x1, y1 = slide_rect
    y_ranges = _tile_boundaries(y0, y1, feature_config.tile_rows)
    x_ranges = _tile_boundaries(x0, x1, feature_config.tile_cols)
    raw_dim = 3 + 3 * feature_config.hist_bins
    projection = _projection_matrix(feature_config, raw_dim)
    tiles: List[Dict[str, Any]] = []
    block_index = 0
    for y_start, y_end in y_ranges:
        for x_start, x_end in x_ranges:
            tile = canvas[y_start:y_end, x_start:x_end, :]
            mean = tile.reshape(-1, 3).mean(axis=0, dtype=np.float64) / 255.0
            hist_parts = []
            for channel in range(3):
                counts = np.bincount(tile[:, :, channel].reshape(-1), minlength=256)
                edges = np.linspace(0, 256, feature_config.hist_bins + 1, dtype=np.int64)
                channel_hist = np.empty(feature_config.hist_bins, dtype=np.float64)
                for bin_index in range(feature_config.hist_bins):
                    lo = int(edges[bin_index])
                    hi = int(edges[bin_index + 1])
                    channel_hist[bin_index] = float(counts[lo:hi].sum())
                total = float(max(1, tile.shape[0] * tile.shape[1]))
                hist_parts.append(channel_hist / total)
            histogram = np.concatenate(hist_parts).astype(np.float64)
            weighted = np.concatenate(
                [
                    mean * np.sqrt(feature_config.mean_weight),
                    histogram * np.sqrt(feature_config.hist_weight),
                ]
            )
            vector = _unit(weighted @ projection)
            mean_values = [float_text(float(value)) for value in mean]
            hist_values = [float_text(float(value)) for value in histogram]
            vector_hash = digest(
                {
                    "algorithm": VISUAL_VERSION,
                    "block_index": block_index,
                    "bounds": [x_start, y_start, x_end, y_end],
                    "mean": mean_values,
                    "histogram": hist_values,
                    "vector": f64_b64(vector),
                }
            )
            tiles.append(
                {
                    "block_index": block_index,
                    "bounds": [x_start, y_start, x_end, y_end],
                    "mean": mean_values,
                    "histogram": hist_values,
                    "vector_f64_b64": f64_b64(vector),
                    "tile_hash": vector_hash,
                }
            )
            block_index += 1

    artifact = {
        "algorithm": VISUAL_VERSION,
        "decoder_algorithm": DECODER_VERSION,
        "tile_grid": [feature_config.tile_rows, feature_config.tile_cols],
        "hist_bins": feature_config.hist_bins,
        "embedding_dim": TEXT_EMBEDDING_DIM,
        "tiles": tiles,
    }
    artifact["artifact_hash"] = digest(artifact)
    return artifact
