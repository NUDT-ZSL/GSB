from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from .bundle import ModelLayer
from .errors import DeployError, E_QUANT_SCHEME
from .util import array_digest, encode_array, float_hex


_SCALE_HINT_RE = re.compile(r"^(per_tensor|per_channel)(?::group=([1-9][0-9]*))?$")
_DTYPE_BITS = {"int8": 8, "int4": 4}


@dataclass(frozen=True)
class QuantScheme:
    quant_dtype: str
    bits: int
    qmin: int
    qmax: int
    granularity: str
    group_size_requested: int | None
    group_size: int | None
    zero_point: int

    @property
    def effective_scale_hint(self) -> str:
        base = self.granularity
        if self.group_size is not None:
            return f"{base}:group={self.group_size}"
        return base


def parse_quant_scheme(
    layer: ModelLayer,
    *,
    group_size_cap: int | None = None,
    quant_dtype_override: str | None = None,
) -> QuantScheme:
    dtype = (quant_dtype_override or layer.quant_dtype).strip().lower()
    if dtype not in _DTYPE_BITS:
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            f"unsupported quant_dtype {dtype!r}; expected int8 or int4",
            layer_id=layer.layer_id,
            field="quant_dtype",
            details={"value": dtype, "allowed": ["int4", "int8"]},
        )
    match = _SCALE_HINT_RE.match(layer.scale_hint.strip())
    if not match:
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            f"unsupported scale_hint {layer.scale_hint!r}; expected "
            "per_tensor or per_channel with optional :group=N",
            layer_id=layer.layer_id,
            field="scale_hint",
            details={"value": layer.scale_hint},
        )
    granularity = match.group(1)
    requested = int(match.group(2)) if match.group(2) else None
    effective = requested
    if requested is not None and group_size_cap is not None:
        effective = min(requested, int(group_size_cap))
    bits = _DTYPE_BITS[dtype]
    qmin = -(1 << (bits - 1))
    qmax = (1 << (bits - 1)) - 1
    zero_point = int(layer.zero_point)
    if not (qmin < zero_point < qmax):
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            f"zero_point {zero_point} must satisfy {qmin} < zp < {qmax}",
            layer_id=layer.layer_id,
            field="zero_point",
            details={"value": zero_point, "qmin": qmin, "qmax": qmax},
        )
    return QuantScheme(
        quant_dtype=dtype,
        bits=bits,
        qmin=qmin,
        qmax=qmax,
        granularity=granularity,
        group_size_requested=requested,
        group_size=effective,
        zero_point=zero_point,
    )


def _scale_for_segment(segment: np.ndarray, scheme: QuantScheme) -> float:
    lo = float(np.min(segment))
    hi = float(np.max(segment))
    if not math.isfinite(lo) or not math.isfinite(hi):
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            "weight contains non-finite values",
            field="weights",
        )
    candidates = [
        hi / float(scheme.qmax - scheme.zero_point),
        lo / float(scheme.qmin - scheme.zero_point),
    ]
    scale = max(candidates)
    if not math.isfinite(scale) or scale <= 0.0:
        return 1.0
    return float(scale)


def _units_for(weight: np.ndarray, scheme: QuantScheme) -> list[np.ndarray]:
    if scheme.granularity == "per_tensor":
        return [weight.reshape(-1)]
    return [weight[row, :] for row in range(weight.shape[0])]


def quantize_array(
    weight: np.ndarray,
    scheme: QuantScheme,
) -> tuple[np.ndarray, np.ndarray, list[float], dict[str, Any]]:
    weight = np.ascontiguousarray(weight, dtype="<f8")
    if weight.ndim != 2:
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            f"weight must be 2-D, got shape {weight.shape}",
            field="layer_shape",
        )
    scales: list[float] = []
    dequant = np.empty_like(weight, dtype="<f8")
    quantized = np.empty(weight.shape, dtype=np.int64)
    unit_length = (
        int(weight.size) if scheme.granularity == "per_tensor" else int(weight.shape[1])
    )
    group_size = scheme.group_size or unit_length
    group_count_per_unit = int(math.ceil(unit_length / group_size))

    for unit_index, unit in enumerate(_units_for(weight, scheme)):
        dequant_unit = dequant.reshape(-1) if scheme.granularity == "per_tensor" else dequant[unit_index]
        quant_unit = quantized.reshape(-1) if scheme.granularity == "per_tensor" else quantized[unit_index]
        for group_index in range(group_count_per_unit):
            start = group_index * group_size
            end = min(start + group_size, unit_length)
            segment = np.asarray(unit[start:end], dtype="<f8")
            scale = _scale_for_segment(segment, scheme)
            q = np.rint(segment / scale).astype(np.int64) + scheme.zero_point
            q = np.clip(q, scheme.qmin, scheme.qmax)
            dq = (q.astype("<f8") - float(scheme.zero_point)) * scale
            quant_unit[start:end] = q
            dequant_unit[start:end] = dq
            scales.append(scale)

    quant_error = np.abs(dequant - weight)
    meta = {
        "schema": 1,
        "quant_dtype": scheme.quant_dtype,
        "bits": scheme.bits,
        "qmin": scheme.qmin,
        "qmax": scheme.qmax,
        "zero_point": scheme.zero_point,
        "granularity": scheme.granularity,
        "scale_hint": scheme.effective_scale_hint,
        "scale_hint_requested": (
            f"{scheme.granularity}:group={scheme.group_size_requested}"
            if scheme.group_size_requested is not None
            else scheme.granularity
        ),
        "group_size": group_size,
        "group_size_requested": scheme.group_size_requested,
        "unit_count": len(_units_for(weight, scheme)),
        "unit_length": unit_length,
        "groups_per_unit": group_count_per_unit,
        "scale_count": len(scales),
        "scales_hex": [float_hex(value) for value in scales],
        "weight_shape": list(weight.shape),
        "weight_digest": array_digest(weight),
        "dequant_digest": array_digest(dequant),
        "quantized_digest": array_digest(quantized.astype("<f8")),
        "max_abs_error_hex": float_hex(float(np.max(quant_error))),
        "mse_hex": float_hex(float(np.mean(np.square(quant_error)))),
        "macs": int(weight.size * 2),
    }
    return quantized, dequant, scales, meta


def quantize_layer(
    layer: ModelLayer,
    weight: np.ndarray,
    bias: np.ndarray,
    *,
    group_size_cap: int | None = None,
    quant_dtype_override: str | None = None,
) -> dict[str, Any]:
    scheme = parse_quant_scheme(
        layer,
        group_size_cap=group_size_cap,
        quant_dtype_override=quant_dtype_override,
    )
    _, dequant, _, meta = quantize_array(weight, scheme)
    if tuple(bias.shape) != (layer.out_features,):
        raise DeployError(
            E_QUANT_SCHEME,
            "quantization",
            f"bias shape {tuple(bias.shape)} != ({layer.out_features},)",
            layer_id=layer.layer_id,
            field="bias",
        )
    meta["bias_digest"] = array_digest(bias)
    return {
        "schema": 1,
        "layer_id": layer.layer_id,
        "layer_index": layer.layer_index,
        "weight_revision": layer.weight_revision,
        "quant": meta,
        "dequant": encode_array(dequant),
        "bias": encode_array(bias),
    }


def verify_dequant_with_original(
    layer: ModelLayer,
    weight: np.ndarray,
    artifact: dict[str, Any],
    *,
    group_size_cap: int | None = None,
) -> bool:
    meta = artifact["quant"]
    effective_hint = meta["scale_hint"]
    from dataclasses import replace

    replay_layer = replace(layer, scale_hint=effective_hint)
    scheme = parse_quant_scheme(replay_layer, group_size_cap=None)
    _, dequant, _, replay_meta = quantize_array(weight, scheme)
    checks = [
        replay_meta["scales_hex"] == meta["scales_hex"],
        replay_meta["dequant_digest"] == meta["dequant_digest"],
        array_digest(dequant) == meta["dequant_digest"],
        replay_meta["quantized_digest"] == meta["quantized_digest"],
    ]
    return all(checks)

