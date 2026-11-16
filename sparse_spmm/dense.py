"""Deterministic synthetic dense matrices and input validation."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .errors import ConfigError, NonFiniteInputError, ShapeMismatchError
from .utils import derived_seed


def generate_dense(
    shape: tuple[int, int],
    *,
    seed: int,
    label: str,
    spec: dict[str, Any],
) -> np.ndarray:
    rows, cols = shape
    if rows <= 0 or cols <= 0:
        raise ConfigError(
            "dense matrix dimensions must be positive",
            stage="dense-generation-config",
            details={"shape": [rows, cols]},
        )
    rng = np.random.default_rng(derived_seed(seed, label))
    mode = spec.get("mode", "log_uniform")
    if mode == "zeros":
        return np.zeros((rows, cols), dtype=np.float32)
    if mode != "log_uniform":
        raise ConfigError(
            "dense mode must be zeros or log_uniform",
            stage="dense-generation-config",
            details={"mode": repr(mode)},
        )
    exp_min = float(spec.get("exponent_min", -4.0))
    exp_max = float(spec.get("exponent_max", 4.0))
    negative_ratio = float(spec.get("negative_ratio", 0.5))
    scale = float(spec.get("scale", 1.0))
    if exp_max < exp_min or not math.isfinite(exp_min) or not math.isfinite(exp_max):
        raise ConfigError(
            "dense exponent bounds are invalid",
            stage="dense-generation-config",
            details={"exponent_min": exp_min, "exponent_max": exp_max},
        )
    if not 0.0 <= negative_ratio <= 1.0:
        raise ConfigError(
            "dense negative_ratio must be in [0, 1]",
            stage="dense-generation-config",
            details={"negative_ratio": negative_ratio},
        )
    exponents = rng.uniform(exp_min, exp_max, size=(rows, cols))
    signs = np.where(rng.random(size=(rows, cols)) < negative_ratio, -1.0, 1.0)
    values = scale * signs * np.power(10.0, exponents)
    return values.astype(np.float32)


def generate_b(config: dict[str, Any], *, K: int, N: int) -> np.ndarray:
    spec = config.get("dense_b", config.get("B", {}))
    if not isinstance(spec, dict):
        raise ConfigError("dense_b must be an object", stage="dense-generation-config")
    return generate_dense(
        (K, N),
        seed=int(config.get("seed", 0)),
        label="dense-b",
        spec=spec,
    )


def generate_c(config: dict[str, Any], *, M: int, N: int) -> np.ndarray:
    spec = config.get("dense_c", config.get("C", {"mode": "zeros"}))
    if not isinstance(spec, dict):
        raise ConfigError("dense_c must be an object", stage="dense-generation-config")
    return generate_dense(
        (M, N),
        seed=int(config.get("seed", 0)),
        label="dense-c",
        spec=spec,
    )


def validate_shapes(
    csr: CSRMatrix,
    B: np.ndarray,
    C: np.ndarray,
    *,
    stage: str = "shape-validation",
) -> None:
    M, K = csr.shape
    shape = {"A": [M, K], "B": list(B.shape), "C": list(C.shape)}
    if B.ndim != 2 or B.shape[0] != K or B.shape[1] <= 0:
        raise ShapeMismatchError(
            "B must have shape [K, N] with N > 0",
            stage=stage,
            shape=shape,
        )
    N = int(B.shape[1])
    if C.ndim != 2 or C.shape != (M, N):
        raise ShapeMismatchError(
            "C must have shape [M, N]",
            stage=stage,
            shape=shape,
            details={"expected_C": [M, N]},
        )
    if B.dtype != np.float32 or C.dtype != np.float32:
        raise ShapeMismatchError(
            "dense B and C must be float32",
            stage=stage,
            shape=shape,
            details={"B_dtype": str(B.dtype), "C_dtype": str(C.dtype)},
        )


def validate_finite(
    csr: CSRMatrix,
    B: np.ndarray,
    C: np.ndarray,
    *,
    stage: str = "finite-validation",
) -> None:
    shape = {"A": [csr.rows, csr.cols], "B": list(B.shape), "C": list(C.shape)}
    if not np.all(np.isfinite(csr.data)):
        pos = int(np.flatnonzero(~np.isfinite(csr.data))[0])
        raise NonFiniteInputError(
            "CSR data contains a non-finite value",
            stage=stage,
            shape=shape,
            details={"array": "A.data", "position": pos, "value": repr(float(csr.data[pos]))},
        )
    if not np.all(np.isfinite(B)):
        row, col = (int(item) for item in np.argwhere(~np.isfinite(B))[0])
        raise NonFiniteInputError(
            "B contains a non-finite value",
            stage=stage,
            shape=shape,
            details={"array": "B", "row": row, "col": col, "value": repr(float(B[row, col]))},
        )
    if not np.all(np.isfinite(C)):
        row, col = (int(item) for item in np.argwhere(~np.isfinite(C))[0])
        raise NonFiniteInputError(
            "C contains a non-finite value",
            stage=stage,
            shape=shape,
            details={"array": "C", "row": row, "col": col, "value": repr(float(C[row, col]))},
        )

