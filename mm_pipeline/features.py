"""Deterministic visual and text feature extraction plus quantization."""
from __future__ import annotations

import hashlib

import numpy as np

from .config import PipelineConfig
from .sample import (ATTRIBUTES, COLORS, ORIENTATIONS, SHAPES, SIZES,
                     TEXTURES, Sample)

# Test-only hook: mapping sample_name -> stage name that must raise once.
FORCE_FAILURE: dict[str, str] = {}


class FeatureError(RuntimeError):
    """Raised when feature extraction fails for a sample."""


def _stable_unit_vector(tag: str, dim: int) -> np.ndarray:
    values = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(f"{tag}#{counter}".encode("utf-8")).digest()
        for byte in digest:
            values.append((byte / 255.0) * 2.0 - 1.0)
            if len(values) == dim:
                break
        counter += 1
    vec = np.asarray(values[:dim], dtype=np.float64)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image.astype(np.float64)
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float64)
    return image[:, :, :3].astype(np.float64) @ weights


def extract_visual_features(sample: Sample, cfg: PipelineConfig) -> dict:
    """Extract deterministic per-region feature vectors and bounding boxes."""
    if FORCE_FAILURE.get(sample.name) == "feature_extraction":
        raise FeatureError(f"forced feature failure for {sample.name}")

    image = sample.image
    height, width = image.shape[:2]
    rows, cols = cfg.grid_rows, cfg.grid_cols
    gray = _to_gray(image)
    grad_y, grad_x = np.gradient(gray)
    grad_mag = np.sqrt(grad_x ** 2 + grad_y ** 2)
    grad_orient = np.arctan2(grad_y, grad_x)
    orientation_edges = np.linspace(-np.pi, np.pi, 5)
    orient_index = np.clip(np.digitize(grad_orient, orientation_edges) - 1,
                           0, 3)

    dim_per_block = max(1, cfg.feature_dim // 3)
    records = []
    region_order = []
    for row in range(rows):
        for col in range(cols):
            region_id = f"r{row}c{col}"
            region_order.append(region_id)
            y0 = round(row * height / rows)
            y1 = round((row + 1) * height / rows)
            x0 = round(col * width / cols)
            x1 = round((col + 1) * width / cols)
            block = image[y0:y1, x0:x1]
            block_gray = gray[y0:y1, x0:x1]
            block_mag = grad_mag[y0:y1, x0:x1]

            if image.ndim == 3:
                mean_rgb = block[:, :, :3].reshape(-1, 3).mean(axis=0) / 255.0
                mean_stats = [float(mean_rgb[0]), float(mean_rgb[1]),
                              float(mean_rgb[2]),
                              float(block_gray.mean() / 255.0)]
            else:
                mean_stats = [float(block_gray.mean() / 255.0)] * 4

            mean_vec = _stable_unit_vector(
                f"mean|{sample.name}|{region_id}", dim_per_block)
            mean_seed = float(block_gray.mean() / 255.0)
            mean_vec = mean_vec * (0.25 + 0.75 * mean_seed)

            grad_strength = float(block_mag.mean() / (block_mag.max() + 1e-12))
            grad_vec = _stable_unit_vector(
                f"gradient|{sample.name}|{region_id}", dim_per_block)
            grad_vec = grad_vec * (0.25 + 0.75 * grad_strength)

            hist = np.zeros(4, dtype=np.float64)
            block_orient = orient_index[y0:y1, x0:x1]
            for bin_index in range(4):
                hist[bin_index] = float(
                    (block_orient == bin_index).sum())
            if hist.sum() > 0:
                hist = hist / hist.sum()
            hist_pad = np.zeros(dim_per_block, dtype=np.float64)
            hist_pad[:min(4, dim_per_block)] = hist[:dim_per_block]

            vector = np.concatenate([mean_vec, grad_vec, hist_pad])
            if vector.size < cfg.feature_dim:
                vector = np.concatenate([vector, np.zeros(
                    cfg.feature_dim - vector.size, dtype=np.float64)])
            vector = vector[:cfg.feature_dim]

            # Deterministic discretization: quantization levels change output.
            quantized = np.floor(
                (np.clip(vector, 0.0, 1.0) * cfg.quantization_levels)
            ) / cfg.quantization_levels

            region_attrs = {
                "color": _dominant_color(block),
                "shape": _shape_signature(block, grad_strength, hist),
                "size": _size_signature(block_mag),
                "orientation": _orientation_signature(hist),
                "texture": _texture_signature(block),
            }
            records.append({
                "region_id": region_id,
                "bbox": [x0, y0, x1, y1],
                "vector": vector,
                "quantized": quantized,
                "mean_stats": mean_stats,
                "gradient_strength": grad_strength,
                "orientation_histogram": [float(v) for v in hist],
                "attributes": region_attrs,
            })
    return {"regions": records, "region_order": region_order}


def _dominant_color(block: np.ndarray) -> str:
    if block.ndim != 3:
        return "red"
    pixels = block[:, :, :3].reshape(-1, 3).astype(np.int16)
    saturated = pixels[(pixels.max(axis=1) - pixels.min(axis=1)) > 50]
    if saturated.shape[0] < 8:
        return "yellow"
    r, g, b = saturated.mean(axis=0)
    if r > g and r > b:
        return "red"
    if g > r and g > b:
        return "green"
    if b > r and b > g:
        return "blue"
    return "yellow"


def _shape_signature(block: np.ndarray, grad_strength: float,
                     hist: np.ndarray) -> str:
    # Use the saturated figure mask and deterministic occupancy geometry.
    if block.ndim != 3:
        return "rectangle"
    pixels = block[:, :, :3].astype(np.int16)
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    mask = spread > 50
    occupancy = float(mask.mean())
    if occupancy < 0.05:
        return "line"
    row_h = int(mask.any(axis=1).sum())
    col_w = int(mask.any(axis=0).sum())
    aspect = col_w / max(row_h, 1)
    if aspect > 1.8 or aspect < 0.55:
        return "line"
    widths = [int(mask[r].sum()) for r in range(mask.shape[0])
              if mask[r].any()]
    # The drawn triangle points downward: active width shrinks top -> bottom.
    if widths and widths[0] >= widths[-1] * 1.8 and occupancy < 0.55:
        return "triangle"
    if occupancy >= 0.42:
        return "rectangle"
    return "circle"


def _size_signature(block_mag: np.ndarray) -> str:
    active = float((block_mag > np.percentile(block_mag, 70)).mean())
    if active < 0.15:
        return "small"
    if active < 0.28:
        return "medium"
    return "large"


def _orientation_signature(hist: np.ndarray) -> str:
    horizontal = float(hist[1])
    vertical = float(hist[3])
    if max(horizontal, vertical) < 0.2:
        return "diagonal"
    return "horizontal" if horizontal >= vertical else "vertical"


def _texture_signature(block: np.ndarray) -> str:
    gray = _to_gray(block)
    spread = float(gray.std())
    if spread < 16:
        return "solid"
    if spread < 38:
        return "striped"
    return "noisy"


def extract_text_features(sample: Sample, cfg: PipelineConfig) -> dict:
    """Map each text assertion to a deterministic attribute feature vector."""
    if FORCE_FAILURE.get(sample.name) == "feature_extraction":
        raise FeatureError(f"forced feature failure for {sample.name}")

    value_maps = {
        "color": COLORS,
        "shape": SHAPES,
        "size": SIZES,
        "orientation": ORIENTATIONS,
        "texture": TEXTURES,
    }
    records = []
    for order, assertion in enumerate(sample.assertions):
        attribute = str(assertion["attribute"])
        value = str(assertion["value"])
        attr_index = ATTRIBUTES.index(attribute)
        value_index = value_maps[attribute].index(value)
        vec = _stable_unit_vector(
            f"assert|{attribute}|{value}", cfg.feature_dim)
        # Deterministic mild positive shift keeps matching evidence positive.
        vec = vec * 0.5 + (attr_index + 1) / (len(ATTRIBUTES) * 4)
        records.append({
            "assertion_id": str(assertion["assertion_id"]),
            "order": order,
            "attribute": attribute,
            "value": value,
            "confidence": float(assertion.get("confidence", 0.75)),
            "vector": vec,
        })
    return {"assertions": records}


def attribute_compatibility(assertion: dict, region: dict) -> float:
    """Return a deterministic [-1, 1] attribute compatibility score."""
    expected = region["attributes"].get(assertion["attribute"])
    if expected is None:
        return 0.0
    if expected == assertion["value"]:
        return 1.0
    return -1.0
