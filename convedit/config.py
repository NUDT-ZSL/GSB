"""Execution configuration and operator implementation versions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import BusinessError, E_PARAM_OUT_OF_RANGE
from .util import sha256_json


OPERATOR_IMPLEMENTATION_VERSION = "convedit-ops-1.0.0"
OPERATOR_VERSIONS = {
    "conv2d": f"{OPERATOR_IMPLEMENTATION_VERSION}:conv2d",
    "bilinear": f"{OPERATOR_IMPLEMENTATION_VERSION}:bilinear",
    "nearest": f"{OPERATOR_IMPLEMENTATION_VERSION}:nearest",
}
SUPPORTED_OPS = frozenset(OPERATOR_VERSIONS)
ALIGN_MODES = frozenset({"half_pixel", "asymmetric", "align_corners"})


@dataclass(frozen=True)
class RuntimeConfig:
    raw: dict[str, Any]
    max_workers: int
    conv_padding_value: float
    tensor_layout: str
    compute_dtype: str
    fingerprint: str


def parse_config(raw: Any) -> RuntimeConfig:
    if not isinstance(raw, dict):
        raise BusinessError(E_PARAM_OUT_OF_RANGE, "config must be a JSON object", stage="config")
    max_workers = raw.get("max_workers", 4)
    if not isinstance(max_workers, int) or isinstance(max_workers, bool) or not 1 <= max_workers <= 64:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "max_workers must be an integer in [1, 64]",
            stage="config",
            details={"max_workers": max_workers},
        )
    padding = raw.get("conv_padding_value", 0.0)
    if isinstance(padding, bool) or not isinstance(padding, (int, float)) or not math.isfinite(float(padding)):
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "conv_padding_value must be finite",
            stage="config",
            details={"conv_padding_value": padding},
        )
    layout = raw.get("tensor_layout", "NCHW")
    if layout != "NCHW":
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "only NCHW tensor_layout is supported",
            stage="config",
            details={"tensor_layout": layout},
        )
    dtype = raw.get("compute_dtype", "float64")
    if dtype != "float64":
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "only float64 compute_dtype is supported",
            stage="config",
            details={"compute_dtype": dtype},
        )
    normalized = {
        "max_workers": max_workers,
        "conv_padding_value": float(padding),
        "tensor_layout": layout,
        "compute_dtype": dtype,
        "operator_implementation_version": OPERATOR_IMPLEMENTATION_VERSION,
    }
    if "node_delay_ms" in raw:
        delays = raw["node_delay_ms"]
        if not isinstance(delays, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for key, value in delays.items()
        ):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "node_delay_ms must map node IDs to non-negative finite milliseconds",
                stage="config",
            )
        normalized["node_delay_ms"] = {str(k): float(v) for k, v in delays.items()}
    return RuntimeConfig(
        raw=normalized,
        max_workers=max_workers,
        conv_padding_value=float(padding),
        tensor_layout=layout,
        compute_dtype=dtype,
        fingerprint=sha256_json(normalized),
    )
