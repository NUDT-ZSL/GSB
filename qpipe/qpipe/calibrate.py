"""Offline calibration statistics computed over the calibration split."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import numpy as np

from .deterministic import checksum_array
from .errors import E_SHAPE_MISMATCH, PipelineError
from .models import LayerSpec


@dataclass(frozen=True)
class CalibrationStats:
    layer_index: int
    layer_id: str
    calibration_revision: int
    sample_count: int
    mean: float
    std: float
    minimum: float
    maximum: float
    abs_max: float
    percentile_99: float
    activation_checksum: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_index": self.layer_index,
            "layer_id": self.layer_id,
            "calibration_revision": self.calibration_revision,
            "sample_count": self.sample_count,
            "mean": self.mean,
            "std": self.std,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "abs_max": self.abs_max,
            "percentile_99": self.percentile_99,
            "activation_checksum": self.activation_checksum,
        }


def validate_calibration_shape(spec: LayerSpec, tensor: np.ndarray) -> None:
    if tensor.ndim != 2 or tensor.shape[1] != spec.in_features:
        raise PipelineError(
            E_SHAPE_MISMATCH,
            (
                f"calibration tensor shape {tuple(tensor.shape)} incompatible "
                f"with layer {spec.layer_id} in_features={spec.in_features}"
            ),
            stage="calibrate",
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
            detail={
                "actual_shape": list(tensor.shape),
                "expected_last_dim": spec.in_features,
            },
        )
    if tensor.shape[0] != spec.calibration_split:
        raise PipelineError(
            E_SHAPE_MISMATCH,
            (
                f"calibration sample count {tensor.shape[0]} != "
                f"calibration_split {spec.calibration_split}"
            ),
            stage="calibrate",
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )


def calibration_stats(
    spec: LayerSpec,
    activations: np.ndarray,
    *,
    calibration_revision: int,
) -> CalibrationStats:
    """Summarise the post-ReLU calibration activations of one layer."""
    flat = activations.astype(np.float64, copy=False)
    return CalibrationStats(
        layer_index=spec.layer_index,
        layer_id=spec.layer_id,
        calibration_revision=int(calibration_revision),
        sample_count=int(activations.shape[0]),
        mean=float(flat.mean()),
        std=float(flat.std()),
        minimum=float(flat.min()),
        maximum=float(flat.max()),
        abs_max=float(np.abs(flat).max()),
        percentile_99=float(np.percentile(flat, 99.0)),
        activation_checksum=checksum_array(
            np.ascontiguousarray(activations, dtype=np.float32)
        ),
    )
