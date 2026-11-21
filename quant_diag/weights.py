from __future__ import annotations

import hashlib

import numpy as np

from .errors import E_WEIGHT_TRUNCATED, BusinessError
from .manifest import LayerSpec
from .util import array_digest


def _seed_for(layer: LayerSpec) -> int:
    material = (
        f"{layer.model_tag}|{layer.layer_id}|{layer.weight_revision}|"
        f"{layer.calib_seed}|weights-v1"
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


def generate_weight(layer: LayerSpec) -> np.ndarray:
    rng = np.random.default_rng(_seed_for(layer))
    rows, columns = layer.layer_shape
    base = rng.standard_normal((rows, columns), dtype=np.float64)
    row_scale = 0.22 + 0.015 * np.arange(rows, dtype=np.float64)
    weight = base * row_scale[:, None]
    if weight.size:
        outlier_count = max(1, weight.size // 257)
        outlier_indices = rng.choice(weight.size, size=outlier_count, replace=False)
        flat = weight.reshape(-1)
        flat[outlier_indices] *= 4.5
    return np.ascontiguousarray(weight, dtype=np.float64)


def weight_checksum(weight: np.ndarray) -> str:
    return array_digest(weight)


def prepare_weight(
    layer: LayerSpec, *, truncate: bool = False, truncate_by: int = 1
) -> tuple[np.ndarray, str]:
    weight = generate_weight(layer)
    if truncate:
        flattened = weight.reshape(-1)
        if truncate_by <= 0:
            truncate_by = 1
        truncated = flattened[: max(0, flattened.size - truncate_by)]
        raise BusinessError(
            E_WEIGHT_TRUNCATED,
            "injected weight array truncation detected",
            stage="layer_quantization",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "expected_elements": int(flattened.size),
                "actual_elements": int(truncated.size),
                "expected_shape": list(layer.layer_shape),
            },
            trace=["quantize_layer:shape_guard"],
        )
    return weight, weight_checksum(weight)
