from __future__ import annotations

from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import E_RESOURCE_LIMIT, BusinessError
from .manifest import LayerSpec
from .util import json_digest


def calibration_statistics(
    vectors: np.ndarray,
    layer: LayerSpec,
    config: EngineConfig,
    *,
    sample_digest: str,
) -> dict[str, Any]:
    if vectors.ndim != 2 or vectors.shape[1] != layer.input_dim:
        raise BusinessError(
            "E_CALIBRATION_SHAPE_MISMATCH",
            "calibration matrix shape does not match the layer",
            stage="calibration_statistics",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "matrix_shape": list(vectors.shape),
                "expected_input_dim": layer.input_dim,
            },
        )
    comparisons = int(vectors.size)
    if comparisons > config.max_calibration_comparisons:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "calibration comparison budget exceeded",
            stage="calibration_statistics",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "comparisons": comparisons,
                "max_calibration_comparisons": config.max_calibration_comparisons,
            },
            trace=["calibration_statistics:comparison_budget"],
        )
    flattened = vectors.reshape(-1)
    qmin, qmax = np.quantile(flattened, [0.05, 0.95]).tolist()
    stats: dict[str, Any] = {
        "layer_id": layer.layer_id,
        "layer_index": layer.layer_index,
        "sample_digest": sample_digest,
        "sample_count": int(vectors.shape[0]),
        "input_dim": int(vectors.shape[1]),
        "comparisons": comparisons,
        "mean": float(np.mean(flattened)),
        "std": float(np.std(flattened)),
        "min": float(np.min(flattened)),
        "max": float(np.max(flattened)),
        "absmax": float(np.max(np.abs(flattened))),
        "p05": float(qmin),
        "p95": float(qmax),
        "checksum": "",
    }
    payload = {key: value for key, value in stats.items() if key != "checksum"}
    stats["checksum"] = json_digest(payload)
    return stats
