"""Per-layer deterministic integer quantisation and de-quantisation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import numpy as np

from .deterministic import (
    QUANT_RANGES,
    checksum_array,
    estimate_dequant_bytes,
    estimate_quant_bytes,
)
from .errors import (
    E_INCONSISTENT,
    E_SCALE_INVALID,
    E_SHAPE_MISMATCH,
    E_ZP_OUT_OF_RANGE,
    PipelineError,
)
from .models import LayerSpec


@dataclass(frozen=True)
class QuantResult:
    """The full quantisation artefact for one layer."""

    spec: LayerSpec
    quant_values: np.ndarray
    dequant: np.ndarray
    scale: float
    zero_point: int
    max_abs_error: float
    mean_abs_error: float
    rmse: float
    snr_db: float
    weight_checksum: str
    quant_checksum: str
    dequant_checksum: str
    quant_bytes: int
    dequant_bytes: int

    def params_dict(self) -> Dict[str, Any]:
        """Deterministic quantisation parameter record (no large arrays)."""
        return {
            "layer_id": self.spec.layer_id,
            "layer_index": self.spec.layer_index,
            "weight_revision": self.spec.weight_revision,
            "quant_dtype": self.spec.quant_dtype,
            "scale": self.scale,
            "zero_point": self.zero_point,
            "max_abs_error": self.max_abs_error,
            "mean_abs_error": self.mean_abs_error,
            "rmse": self.rmse,
            "snr_db": self.snr_db,
            "weight_checksum": self.weight_checksum,
            "quant_checksum": self.quant_checksum,
            "dequant_checksum": self.dequant_checksum,
            "quant_bytes": self.quant_bytes,
            "dequant_bytes": self.dequant_bytes,
        }


def validate_weight_shape(spec: LayerSpec, weight: np.ndarray) -> None:
    if tuple(weight.shape) != tuple(spec.layer_shape):
        raise PipelineError(
            E_SHAPE_MISMATCH,
            (
                f"weight shape {tuple(weight.shape)} != manifest "
                f"{tuple(spec.layer_shape)}"
            ),
            stage="validate",
            model_tag=spec.model_tag,
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
            detail={
                "actual_shape": list(weight.shape),
                "expected_shape": list(spec.layer_shape),
            },
        )
    if weight.size != spec.numel():
        raise PipelineError(
            E_SHAPE_MISMATCH,
            f"weight length {weight.size} != expected {spec.numel()}",
            stage="validate",
            model_tag=spec.model_tag,
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )


def quantize_layer(spec: LayerSpec, weight: np.ndarray) -> QuantResult:
    """Quantise one weight matrix deterministically.

    Steps (all float32 until the final integer cast):

    1. validate shape, scale and zero point;
    2. ``q = round(weight / scale) + zero_point`` and clip to dtype range;
    3. de-quantise ``w' = (q - zero_point) * scale``;
    4. measure error statistics and content checksums.
    """
    validate_weight_shape(spec, weight)
    if not (spec.scale_hint > 0.0) or not np.isfinite(spec.scale_hint):
        raise PipelineError(
            E_SCALE_INVALID,
            f"scale_hint must be positive finite, got {spec.scale_hint}",
            stage="quantize",
            model_tag=spec.model_tag,
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )
    lo, hi = QUANT_RANGES[spec.quant_dtype]
    if not (lo <= spec.zero_point <= hi):
        raise PipelineError(
            E_ZP_OUT_OF_RANGE,
            f"zero_point {spec.zero_point} not in [{lo},{hi}]",
            stage="quantize",
            model_tag=spec.model_tag,
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )

    scale = np.float32(spec.scale_hint)
    scaled = weight.astype(np.float32, copy=False) / scale
    # Banker-independent deterministic rounding: floor(x + 0.5) (round half up).
    rounded = np.floor(scaled + np.float32(0.5))
    q_real = rounded + np.float32(spec.zero_point)
    q_clipped = np.clip(q_real, lo, hi)
    quant_values = q_clipped.astype(np.result_type(np.int64))

    dequant = (
        quant_values.astype(np.float32) - np.float32(spec.zero_point)
    ) * scale
    dequant = np.ascontiguousarray(dequant, dtype=np.float32)

    error = dequant - weight.astype(np.float32, copy=False)
    abs_error = np.abs(error)
    max_abs_error = float(abs_error.max())
    mean_abs_error = float(abs_error.mean())
    rmse = float(np.sqrt(np.mean(error.astype(np.float64) ** 2)))
    signal_power = float(np.mean(weight.astype(np.float64) ** 2))
    noise_power = float(np.mean(error.astype(np.float64) ** 2))
    if noise_power > 0.0 and signal_power > 0.0:
        snr_db = float(10.0 * np.log10(signal_power / noise_power))
    else:
        snr_db = float("inf")

    return QuantResult(
        spec=spec,
        quant_values=np.ascontiguousarray(quant_values),
        dequant=dequant,
        scale=float(scale),
        zero_point=int(spec.zero_point),
        max_abs_error=max_abs_error,
        mean_abs_error=mean_abs_error,
        rmse=rmse,
        snr_db=snr_db,
        weight_checksum=checksum_array(weight),
        quant_checksum=checksum_array(quant_values),
        dequant_checksum=checksum_array(dequant),
        quant_bytes=estimate_quant_bytes(spec.layer_shape, spec.quant_dtype),
        dequant_bytes=estimate_dequant_bytes(spec.layer_shape),
    )


def verify_roundtrip(
    quant_dtype: str,
    zero_point: int,
    scale: float,
    quant_values: np.ndarray,
    expected_dequant_checksum: str,
    *,
    layer_index: int = 0,
) -> None:
    """Re-derive de-quantised weights from quant params and compare bytes."""
    lo, hi = QUANT_RANGES[quant_dtype]
    if not (lo <= zero_point <= hi):
        raise PipelineError(
            E_ZP_OUT_OF_RANGE,
            "stored zero_point out of range during roundtrip check",
            stage="quantize",
            layer_index=layer_index,
        )
    rebuilt = (
        quant_values.astype(np.float32) - np.float32(zero_point)
    ) * np.float32(scale)
    if checksum_array(np.ascontiguousarray(rebuilt, dtype=np.float32)) != (
        expected_dequant_checksum
    ):
        raise PipelineError(
            E_INCONSISTENT,
            "de-quantised weight cannot be reconstructed from quant params",
            stage="quantize",
            layer_index=layer_index,
        )
