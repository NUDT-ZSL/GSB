from __future__ import annotations

import math
from typing import Any

import numpy as np

from .canonical import content_digest
from .errors import E_FORMAT_CORRUPT, TuningError
from .ids import stable_seed


SHAPE_FIELDS = {
    "matmul": ("M", "N", "K"),
    "conv2d": (
        "batch",
        "height",
        "width",
        "in_channels",
        "out_channels",
        "kernel_h",
        "kernel_w",
    ),
}


def require_fields(obj: dict[str, Any], fields: tuple[str, ...], where: str) -> None:
    missing = [field for field in fields if field not in obj or obj[field] is None]
    if missing:
        raise TuningError(
            E_FORMAT_CORRUPT,
            f"{where} is missing required fields: {', '.join(missing)}",
            {"missing": missing, "where": where},
        )


def normalize_shape(op_kind: str, shape: dict[str, Any]) -> dict[str, int]:
    if op_kind not in SHAPE_FIELDS:
        raise TuningError(E_FORMAT_CORRUPT, f"unsupported op_kind: {op_kind}")
    require_fields(shape, SHAPE_FIELDS[op_kind], "workload_shape")
    normalized = {key: int(shape[key]) for key in SHAPE_FIELDS[op_kind]}
    for key, value in normalized.items():
        if value <= 0:
            raise TuningError(E_FORMAT_CORRUPT, f"shape field {key} must be positive")
    if op_kind == "conv2d":
        normalized["stride"] = int(shape.get("stride", 1))
        normalized["padding"] = int(shape.get("padding", 0))
        normalized["dilation"] = int(shape.get("dilation", 1))
        if min(normalized["stride"], normalized["padding"], normalized["dilation"]) < 0:
            raise TuningError(E_FORMAT_CORRUPT, "stride/padding/dilation must be non-negative")
        effective_kh = normalized["dilation"] * (normalized["kernel_h"] - 1) + 1
        effective_kw = normalized["dilation"] * (normalized["kernel_w"] - 1) + 1
        normalized["out_height"] = (
            normalized["height"] + 2 * normalized["padding"] - effective_kh
        ) // normalized["stride"] + 1
        normalized["out_width"] = (
            normalized["width"] + 2 * normalized["padding"] - effective_kw
        ) // normalized["stride"] + 1
        if normalized["out_height"] <= 0 or normalized["out_width"] <= 0:
            raise TuningError(E_FORMAT_CORRUPT, "conv2d output shape is empty")
    return normalized


def deterministic_arrays(
    tuning_id: str, op_kind: str, shape: dict[str, Any]
) -> dict[str, np.ndarray]:
    dims = normalize_shape(op_kind, shape)
    shape_key = content_digest(dims)
    if op_kind == "matmul":
        m, n, k = dims["M"], dims["N"], dims["K"]
        activation_seed = stable_seed(tuning_id, op_kind, shape_key, "activation")
        weight_seed = stable_seed(tuning_id, op_kind, shape_key, "weight")
        activation = np.random.default_rng(activation_seed).uniform(-1.0, 1.0, (m, k))
        weight = np.random.default_rng(weight_seed).uniform(-0.5, 0.5, (k, n))
        return {"activation": activation, "weight": weight}

    batch = dims["batch"]
    height = dims["height"]
    width = dims["width"]
    in_c = dims["in_channels"]
    out_c = dims["out_channels"]
    kh = dims["kernel_h"]
    kw = dims["kernel_w"]
    activation_seed = stable_seed(tuning_id, op_kind, shape_key, "activation")
    weight_seed = stable_seed(tuning_id, op_kind, shape_key, "weight")
    activation = np.random.default_rng(activation_seed).uniform(
        -1.0, 1.0, (batch, height, width, in_c)
    )
    weight = np.random.default_rng(weight_seed).uniform(
        -0.5, 0.5, (kh, kw, in_c, out_c)
    )
    return {"activation": activation, "weight": weight}


def qbounds(
    bits: int,
    quant_config: dict[str, Any] | None = None,
) -> tuple[int, int]:
    if bits < 2 or bits > 32:
        raise TuningError(E_FORMAT_CORRUPT, f"unsupported fixed-point bits: {bits}")
    if quant_config and "qmin" in quant_config and "qmax" in quant_config:
        return int(quant_config["qmin"]), int(quant_config["qmax"])
    dtype = str((quant_config or {}).get("dtype", "")).lower()
    unsigned = bool((quant_config or {}).get("unsigned", False)) or dtype.startswith("u")
    if not unsigned and quant_config is not None and not dtype:
        zero_point = int(quant_config.get("zero_point", 0))
        unsigned = zero_point >= (1 << (bits - 1))
    if unsigned:
        return 0, (1 << bits) - 1
    return -(1 << (bits - 1)), (1 << (bits - 1)) - 1


def round_half_away_from_zero(values: np.ndarray) -> np.ndarray:
    """Explicit deterministic rounding mode: ties move away from zero."""
    return np.where(values >= 0, np.floor(values + 0.5), np.ceil(values - 0.5))


def quantize_dequantize(
    values: np.ndarray,
    scale: float,
    zero_point: int | float,
    bits: int = 8,
    qmin: int | None = None,
    qmax: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Fixed-point fake quantization.

    q = clip(round_half_away_from_zero(x / scale) + zero_point, qmin, qmax)
    x_hat = (q - zero_point) * scale
    """
    if not math.isfinite(scale) or scale <= 0:
        raise TuningError(E_FORMAT_CORRUPT, "quant_config.scale must be a positive finite number")
    if qmin is None or qmax is None:
        qmin, qmax = qbounds(int(bits), {"zero_point": zero_point})
    scaled = values.astype(np.float64) / float(scale) + float(zero_point)
    rounded = round_half_away_from_zero(scaled)
    clipped = np.clip(rounded, qmin, qmax)
    quantized = clipped.astype(np.int64)
    dequantized = (quantized - int(zero_point)).astype(np.float64) * float(scale)
    return quantized, dequantized


def _array_byte_digest(array: np.ndarray) -> str:
    return "sha256:" + __import__("hashlib").sha256(
        array.astype("<i8", copy=False).tobytes()
    ).hexdigest()


def compute_quant_report(
    tuning_id: str,
    op_kind: str,
    workload_shape: dict[str, Any],
    quant_config: dict[str, Any],
    effective_bits: int | None = None,
) -> dict[str, Any]:
    require_fields(quant_config, ("scale", "zero_point"), "quant_config")
    bits = int(effective_bits or quant_config.get("bits", 8))
    scale = float(quant_config["scale"])
    zero_point = int(quant_config["zero_point"])
    arrays = deterministic_arrays(tuning_id, op_kind, workload_shape)

    original_parts: list[np.ndarray] = []
    restored_parts: list[np.ndarray] = []
    q_digests: dict[str, str] = {}
    clipping: dict[str, int] = {}
    qmin, qmax = qbounds(bits, quant_config)

    for role in ("activation", "weight"):
        original = arrays[role].astype(np.float64)
        quantized, restored = quantize_dequantize(
            original, scale, zero_point, bits, qmin, qmax
        )
        q_digests[role] = _array_byte_digest(quantized)
        clipped_scaled = round_half_away_from_zero(original / scale + zero_point)
        clipping[role] = int(np.count_nonzero((clipped_scaled < qmin) | (clipped_scaled > qmax)))
        original_parts.append(original.ravel())
        restored_parts.append(restored.ravel())

    original_all = np.concatenate(original_parts)
    restored_all = np.concatenate(restored_parts)
    error = restored_all - original_all
    mse = float(np.mean(error * error))
    rmse = math.sqrt(max(0.0, mse))
    signal_rms = math.sqrt(max(0.0, float(np.mean(original_all * original_all))))
    relative_rmse = rmse / signal_rms if signal_rms > 0 else 0.0
    mae = float(np.mean(np.abs(error)))
    max_abs = float(np.max(np.abs(error))) if error.size else 0.0

    metrics = {
        "mae": mae,
        "rmse": rmse,
        "max_abs": max_abs,
        "relative_rmse": relative_rmse,
        "clipped_values": sum(clipping.values()),
        "activation_clipped": clipping["activation"],
        "weight_clipped": clipping["weight"],
        "element_count": int(original_all.size),
    }
    report = {
        "formula": (
            "q=clip(round_half_away_from_zero(x/scale)+zero_point,qmin,qmax); "
            "x_hat=(q-zero_point)*scale"
        ),
        "rounding_mode": "round_half_away_from_zero",
        "tuning_id": tuning_id,
        "op_kind": op_kind,
        "workload_shape": normalize_shape(op_kind, workload_shape),
        "scale": scale,
        "zero_point": zero_point,
        "bits": bits,
        "metrics": metrics,
        "quantized_array_digests": q_digests,
    }
    report["metrics_digest"] = content_digest(metrics)
    return report
