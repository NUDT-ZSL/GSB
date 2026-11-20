"""Deterministic layer-wise forward inference over the MLP snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from .deterministic import checksum_array
from .errors import E_SHAPE_MISMATCH, PipelineError
from .models import LayerSpec


def stable_softmax(logits: np.ndarray) -> np.ndarray:
    """Numerically stable, deterministic softmax over the last axis."""
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


@dataclass(frozen=True)
class LayerActivation:
    layer_index: int
    layer_id: str
    weight_revision: int
    input_checksum: str
    output_checksum: str
    output: np.ndarray

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_index": self.layer_index,
            "layer_id": self.layer_id,
            "weight_revision": self.weight_revision,
            "input_checksum": self.input_checksum,
            "output_checksum": self.output_checksum,
        }


def validate_input_shape(spec: LayerSpec, tensor: np.ndarray) -> None:
    if tensor.ndim != 2 or tensor.shape[1] != spec.in_features:
        raise PipelineError(
            E_SHAPE_MISMATCH,
            (
                f"inference input shape {tuple(tensor.shape)} incompatible "
                f"with layer {spec.layer_id} in_features={spec.in_features}"
            ),
            stage="infer",
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
            detail={
                "actual_shape": list(tensor.shape),
                "expected_last_dim": spec.in_features,
            },
        )


def forward_layer(
    spec: LayerSpec,
    inputs: np.ndarray,
    dequant_weights: np.ndarray,
    *,
    is_last: bool,
) -> LayerActivation:
    """Run one layer: ``x @ W`` then ReLU (hidden) or softmax (last)."""
    validate_input_shape(spec, inputs)
    if tuple(dequant_weights.shape) != tuple(spec.layer_shape):
        raise PipelineError(
            E_SHAPE_MISMATCH,
            "de-quantised weight shape does not match manifest",
            stage="infer",
            layer_id=spec.layer_id,
            layer_index=spec.layer_index,
        )
    x = np.ascontiguousarray(inputs, dtype=np.float32)
    logits = x @ dequant_weights
    if is_last:
        output = stable_softmax(logits.astype(np.float64)).astype(np.float32)
    else:
        output = np.maximum(logits, np.float32(0.0))
    output = np.ascontiguousarray(output, dtype=np.float32)
    return LayerActivation(
        layer_index=spec.layer_index,
        layer_id=spec.layer_id,
        weight_revision=spec.weight_revision,
        input_checksum=checksum_array(x),
        output_checksum=checksum_array(output),
        output=output,
    )


def run_chain(
    specs: List[LayerSpec],
    dequant_by_index: Dict[int, np.ndarray],
    calibration_input: np.ndarray,
    *,
    start_index: int = 0,
    carry: Optional[np.ndarray] = None,
) -> List[LayerActivation]:
    """Run layers ``start_index..end`` chaining each output into the next."""
    current = (
        np.ascontiguousarray(calibration_input, dtype=np.float32)
        if start_index == 0
        else np.ascontiguousarray(carry, dtype=np.float32)
    )
    results: List[LayerActivation] = []
    last_index = max(spec.layer_index for spec in specs)
    for spec in specs:
        if spec.layer_index < start_index:
            continue
        activation = forward_layer(
            spec,
            current,
            dequant_by_index[spec.layer_index],
            is_last=(spec.layer_index == last_index),
        )
        results.append(activation)
        current = activation.output
    return results
