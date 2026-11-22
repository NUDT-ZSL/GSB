from __future__ import annotations

from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .deterministic import calibration_input
from .errors import BusinessError, E_RESOURCE_LIMIT
from .quantization import QuantizedLayer
from .serialization import array_checksum, encode_array


def compute_calibration(
    config: WorkbenchConfig,
    entry: dict[str, Any],
    record: QuantizedLayer,
    *,
    remaining_comparisons: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    layer_id = str(entry["layer_id"])
    layer_index = int(entry["layer_index"])
    output_dim, input_dim = (int(x) for x in entry["layer_shape"])
    comparisons_per_sample = max(1, input_dim * output_dim)
    max_samples = int(remaining_comparisons) // comparisons_per_sample
    if max_samples < 1:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "校准样本比较次数预算不足，至少需要 1 个样本",
            stage="calibration",
            layer_id=layer_id,
            layer_index=layer_index,
            details={
                "remaining_comparisons": remaining_comparisons,
                "comparisons_per_sample": comparisons_per_sample,
            },
        )
    requested_batch = config.calib_batch_size
    used_batch = min(requested_batch, max_samples)
    degradation: dict[str, Any] | None = None
    if used_batch < requested_batch:
        degradation = {
            "category": "calibration_subsample",
            "stage": "calibration",
            "layer_id": layer_id,
            "layer_index": layer_index,
            "original": {"batch_size": requested_batch},
            "applied": {"batch_size": used_batch},
            "reason": "max_calibration_comparisons",
        }
    samples = calibration_input(config, entry, batch_size=used_batch)
    outputs = np.ascontiguousarray(samples @ record.dequantized.T)
    comparison_count = int(used_batch * comparisons_per_sample)
    abs_outputs = np.abs(outputs)
    output_abs_max = float(np.max(abs_outputs)) if outputs.size else 0.0
    qmax = float(max(abs(record.qmin), abs(record.qmax)))
    stats = {
        "layer_id": layer_id,
        "layer_index": layer_index,
        "sample_count": int(used_batch),
        "comparison_count": comparison_count,
        "input_shape": list(samples.shape),
        "output_shape": list(outputs.shape),
        "input_checksum": array_checksum(samples),
        "output_checksum": array_checksum(outputs),
        "input_mean": float(np.mean(samples)),
        "input_std": float(np.std(samples)),
        "output_min": float(np.min(outputs)) if outputs.size else 0.0,
        "output_max": float(np.max(outputs)) if outputs.size else 0.0,
        "output_abs_max": output_abs_max,
        "output_mean": float(np.mean(outputs)) if outputs.size else 0.0,
        "output_std": float(np.std(outputs)) if outputs.size else 0.0,
        "recommended_activation_scale": output_abs_max / qmax if qmax > 0 else 0.0,
        "calib_seed": int(entry["calib_seed"]),
    }
    payload = {
        **stats,
        "samples": encode_array(samples),
        "outputs": encode_array(outputs),
    }
    return payload, degradation
