from __future__ import annotations

from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import E_INVALID_QUANT_DTYPE, BusinessError
from .manifest import LayerSpec
from .util import array_digest, int_array_digest, int_list, json_digest, float_list


QUANT_DTYPES: dict[str, tuple[int, int]] = {
    "int8": (-128, 127),
    "uint8": (0, 255),
    "int4": (-8, 7),
    "uint4": (0, 15),
    "int16": (-32768, 32767),
}


def dtype_range(quant_dtype: str, layer: LayerSpec | None = None) -> tuple[int, int]:
    if quant_dtype not in QUANT_DTYPES:
        raise BusinessError(
            E_INVALID_QUANT_DTYPE,
            f"unsupported quant_dtype: {quant_dtype}",
            stage="layer_quantization",
            layer_id=layer.layer_id if layer else None,
            layer_index=layer.layer_index if layer else None,
            details={"quant_dtype": quant_dtype, "supported": sorted(QUANT_DTYPES)},
            trace=["quantize_layer:dtype_guard"],
        )
    return QUANT_DTYPES[quant_dtype]


def _stored_scale(scale: float, storage_dtype: str) -> float:
    if storage_dtype == "float16":
        return float(np.float16(scale))
    if storage_dtype == "float64":
        return float(scale)
    raise BusinessError(
        "E_INVALID_CONFIG",
        f"unsupported scale storage dtype: {storage_dtype}",
        stage="layer_quantization",
    )


def quantize_weight(
    weight: np.ndarray,
    layer: LayerSpec,
    config: EngineConfig,
    *,
    group_size: int | None = None,
) -> dict[str, Any]:
    expected_shape = layer.layer_shape
    if tuple(weight.shape) != expected_shape:
        raise BusinessError(
            "E_WEIGHT_TRUNCATED",
            "weight shape does not match the manifest",
            stage="layer_quantization",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "expected_shape": list(expected_shape),
                "actual_shape": list(weight.shape),
                "expected_elements": int(np.prod(expected_shape)),
                "actual_elements": int(weight.size),
            },
            trace=["quantize_layer:shape_guard"],
        )
    qmin, qmax = dtype_range(layer.quant_dtype, layer)
    if not (qmin <= layer.zero_point <= qmax):
        raise BusinessError(
            E_INVALID_QUANT_DTYPE,
            "zero_point is outside quant dtype range",
            stage="layer_quantization",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
            details={
                "zero_point": layer.zero_point,
                "range": [qmin, qmax],
                "quant_dtype": layer.quant_dtype,
            },
            trace=["quantize_layer:zero_point_guard"],
        )
    effective_group_size = int(group_size or config.group_size)
    if effective_group_size <= 0:
        raise BusinessError(
            "E_INVALID_CONFIG",
            "group_size must be positive",
            stage="layer_quantization",
            layer_id=layer.layer_id,
            layer_index=layer.layer_index,
        )
    flat = np.ascontiguousarray(weight, dtype=np.float64).reshape(-1)
    codes = np.empty(flat.size, dtype=np.int64)
    stored_dequant = np.empty(flat.size, dtype=np.float64)
    exact_dequant = np.empty(flat.size, dtype=np.float64)
    groups: list[dict[str, Any]] = []
    positive_span = qmax - layer.zero_point
    negative_span = layer.zero_point - qmin
    q_abs = max(positive_span, negative_span, 1)
    for start in range(0, flat.size, effective_group_size):
        end = min(flat.size, start + effective_group_size)
        values = flat[start:end]
        absmax = float(np.max(np.abs(values))) if values.size else 0.0
        base_scale = max(absmax / float(q_abs), 1e-12)
        exact_scale = float(base_scale * layer.scale_hint)
        stored_scale = _stored_scale(exact_scale, config.scale_storage_dtype)
        if stored_scale <= 0.0:
            stored_scale = float(np.nextafter(0.0, 1.0))
        raw = np.rint(values / exact_scale).astype(np.int64) + layer.zero_point
        group_codes = np.clip(raw, qmin, qmax).astype(np.int64)
        codes[start:end] = group_codes
        group_exact = (group_codes.astype(np.float64) - layer.zero_point) * exact_scale
        group_stored = (group_codes.astype(np.float64) - layer.zero_point) * stored_scale
        exact_dequant[start:end] = group_exact
        stored_dequant[start:end] = group_stored
        groups.append(
            {
                "group_index": len(groups),
                "start": start,
                "end": end,
                "count": end - start,
                "scale_exact": exact_scale,
                "scale_stored": stored_scale,
                "zero_point": layer.zero_point,
                "absmax": absmax,
                "qmin": int(np.min(group_codes)) if group_codes.size else 0,
                "qmax": int(np.max(group_codes)) if group_codes.size else 0,
            }
        )
    codes_2d = codes.reshape(layer.layer_shape)
    stored_2d = stored_dequant.reshape(layer.layer_shape)
    exact_2d = exact_dequant.reshape(layer.layer_shape)
    params = {
        "quant_dtype": layer.quant_dtype,
        "qmin": qmin,
        "qmax": qmax,
        "group_size": effective_group_size,
        "scale_hint": layer.scale_hint,
        "zero_point": layer.zero_point,
        "scale_storage_dtype": config.scale_storage_dtype,
        "groups": groups,
    }
    result: dict[str, Any] = {
        "layer_id": layer.layer_id,
        "layer_index": layer.layer_index,
        "layer_shape": list(layer.layer_shape),
        "weight_revision": layer.weight_revision,
        "params": params,
        "codes": int_list(codes),
        "codes_digest": int_array_digest(codes_2d),
        "weight_digest": "",
        "dequant_weights": float_list(stored_2d),
        "dequant_digest": array_digest(stored_2d),
        "exact_dequant_digest": array_digest(exact_2d),
    }
    return result


def _codes_array(quant_layer: dict[str, Any]) -> np.ndarray:
    return np.asarray(quant_layer["codes"], dtype=np.int64)


def dequantize_layer(
    quant_layer: dict[str, Any], *, scale_mode: str = "stored"
) -> np.ndarray:
    if scale_mode not in {"stored", "exact"}:
        raise ValueError(f"unknown scale_mode: {scale_mode}")
    flat = np.empty(len(quant_layer["codes"]), dtype=np.float64)
    codes = _codes_array(quant_layer)
    for group in quant_layer["params"]["groups"]:
        start = int(group["start"])
        end = int(group["end"])
        zero_point = float(group["zero_point"])
        scale = float(
            group["scale_stored"] if scale_mode == "stored" else group["scale_exact"]
        )
        flat[start:end] = (codes[start:end].astype(np.float64) - zero_point) * scale
    return flat.reshape(tuple(quant_layer["layer_shape"]))


def finalize_quant_layer(
    quant_layer: dict[str, Any], weight: np.ndarray, weight_digest: str
) -> dict[str, Any]:
    quant_layer = dict(quant_layer)
    quant_layer["weight_digest"] = weight_digest
    stored = dequantize_layer(quant_layer, scale_mode="stored")
    quant_layer["dequant_weights"] = float_list(stored)
    quant_layer["dequant_digest"] = array_digest(stored)
    payload = {key: value for key, value in quant_layer.items() if key != "checksum"}
    quant_layer["checksum"] = json_digest(payload)
    return quant_layer


def quant_layer_checksum(quant_layer: dict[str, Any]) -> str:
    payload = {key: value for key, value in quant_layer.items() if key != "checksum"}
    return json_digest(payload)
