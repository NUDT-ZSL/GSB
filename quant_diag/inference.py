from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .config import EngineConfig
from .errors import E_INPUT_DIM_MISMATCH, E_LAYER_INCOMPLETE, BusinessError
from .manifest import LayerSpec
from .util import array_digest, float_list, json_digest


def activate(
    weight: np.ndarray, vector: np.ndarray, accumulation_dtype: str
) -> np.ndarray:
    if accumulation_dtype == "float32":
        with np.errstate(over="ignore", invalid="ignore"):
            accumulated = np.asarray(
                weight, dtype=np.float32
            ) @ np.asarray(vector, dtype=np.float32)
            output = np.tanh(accumulated).astype(np.float64)
    elif accumulation_dtype == "float64":
        accumulated = np.asarray(weight, dtype=np.float64) @ np.asarray(
            vector, dtype=np.float64
        )
        output = np.tanh(accumulated)
    else:
        raise BusinessError(
            "E_INVALID_CONFIG",
            f"unsupported accumulation dtype: {accumulation_dtype}",
            stage="layer_inference",
        )
    return np.ascontiguousarray(output, dtype=np.float64)


def infer_one_layer(
    quant_layers: Mapping[str, dict[str, Any]],
    layer: LayerSpec,
    input_vector: np.ndarray,
    config: EngineConfig,
) -> dict[str, Any]:
    quant_layer = quant_layers.get(layer.layer_id)
    if quant_layer is None:
        raise BusinessError(
            E_LAYER_INCOMPLETE,
            "committed quantization parameters are missing for this layer",
            stage="layer_inference",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={"missing_layer": layer.layer_id},
            trace=["infer_one_layer:lookup_quantization"],
        )
    vector = np.asarray(input_vector, dtype=np.float64)
    if vector.ndim != 1 or vector.shape[0] != layer.input_dim:
        raise BusinessError(
            E_INPUT_DIM_MISMATCH,
            "inference input dimension does not match the layer",
            stage="layer_inference",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "input_dim": int(vector.shape[0]) if vector.ndim else 0,
                "expected_input_dim": layer.input_dim,
                "input_shape": list(vector.shape),
            },
            trace=["infer_one_layer:input_dim_guard"],
        )
    weight = np.asarray(
        quant_layer["dequant_weights"], dtype=np.float64
    ).reshape(tuple(layer.layer_shape))
    output = activate(weight, vector, config.accumulation_dtype)
    result: dict[str, Any] = {
        "layer_id": layer.layer_id,
        "layer_index": layer.layer_index,
        "input_dim": layer.input_dim,
        "output_dim": layer.output_dim,
        "input_digest": array_digest(vector),
        "weight_digest": quant_layer["dequant_digest"],
        "quant_checksum": quant_layer["checksum"],
        "activation": float_list(output),
        "activation_digest": array_digest(output),
        "accumulation_dtype": config.accumulation_dtype,
        "checksum": "",
    }
    payload = {key: value for key, value in result.items() if key != "checksum"}
    result["checksum"] = json_digest(payload)
    return result


def independent_replay(
    manifest_layers: tuple[LayerSpec, ...],
    quant_layers: Mapping[str, dict[str, Any]],
    initial_input: np.ndarray,
    config: EngineConfig,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    vector = np.asarray(initial_input, dtype=np.float64)
    records: list[dict[str, Any]] = []
    for layer in manifest_layers:
        record = infer_one_layer(quant_layers, layer, vector, config)
        vector = np.asarray(record["activation"], dtype=np.float64)
        records.append(record)
    return vector, records
