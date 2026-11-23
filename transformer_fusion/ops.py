"""Deterministic operator semantics used by both execution paths."""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np


# name -> (elementwise, reduction)
ACTIVATIONS: dict[str, tuple[bool, bool]] = {
    "relu": (True, False),
    "gelu": (True, False),
    "silu": (True, False),
    "sigmoid": (True, False),
    "tanh": (True, False),
    "exp": (True, False),
    "leaky_relu": (True, False),
    "softmax": (False, True),
}

TRANSPOSE_OPS = frozenset({"transpose", "permute"})
REDUCTION_OPS = frozenset({"softmax", "layernorm", "sum", "mean", "max", "min"})
OTHER_OPS = frozenset({"identity"}) | REDUCTION_OPS
KNOWN_OPS = frozenset(ACTIVATIONS) | TRANSPOSE_OPS | OTHER_OPS


def is_known_op(name: str) -> bool:
    return name in KNOWN_OPS


def is_activation_op(name: str) -> bool:
    return name in ACTIVATIONS


def is_elementwise_activation(name: str) -> bool:
    return bool(ACTIVATIONS.get(name, (False, False))[0])


def is_transpose_op(name: str) -> bool:
    return name in TRANSPOSE_OPS


def is_reduction_op(name: str) -> bool:
    return name in REDUCTION_OPS


def activation(name: str, values: np.ndarray, eps: float) -> np.ndarray:
    """Apply a scalar activation with an explicit deterministic formula.

    ``eps`` is a declared denominator stabilizer for sigmoid/silu.  It is
    intentionally part of the activation caliber and therefore part of the
    fusion cache key.
    """

    x = np.asarray(values)
    if name == "relu":
        return np.maximum(x, np.asarray(0.0, dtype=x.dtype))
    if name == "gelu":
        # tanh approximation, versioned as gelu_tanh_v1.
        coeff = math.sqrt(2.0 / math.pi)
        return 0.5 * x * (1.0 + np.tanh(coeff * (x + 0.044715 * x * x * x)))
    if name in {"sigmoid", "silu"}:
        positive = x >= 0
        z_neg = np.exp(np.minimum(x, 0.0))
        z_pos = np.exp(np.maximum(-x, 0.0))
        sigmoid = np.where(
            positive,
            1.0 / (1.0 + z_pos + eps),
            z_neg / (1.0 + z_neg + eps),
        )
        if name == "sigmoid":
            return sigmoid
        return x * sigmoid
    if name == "tanh":
        return np.tanh(x)
    if name == "exp":
        return np.exp(x)
    if name == "leaky_relu":
        return np.where(x >= 0, x, np.asarray(0.01, dtype=x.dtype) * x)
    if name == "softmax":
        shifted = x - np.max(x, axis=-1, keepdims=True)
        exponent = np.exp(shifted)
        return exponent / np.sum(exponent, axis=-1, keepdims=True)
    raise KeyError(f"unknown activation: {name}")


def apply_op(array: np.ndarray, op_name: str, axis_order: Sequence[int], eps: float) -> np.ndarray:
    if op_name in ACTIVATIONS:
        return activation(op_name, array, eps)
    if op_name in TRANSPOSE_OPS:
        return np.transpose(array, axes=tuple(axis_order))
    if op_name == "identity":
        return array
    if op_name == "layernorm":
        mean = np.mean(array, axis=-1, keepdims=True)
        centered = array - mean
        variance = np.mean(centered * centered, axis=-1, keepdims=True)
        return centered / np.sqrt(variance + eps)
    if op_name == "sum":
        return np.sum(array, axis=-1, keepdims=True)
    if op_name == "mean":
        return np.mean(array, axis=-1, keepdims=True)
    if op_name == "max":
        return np.max(array, axis=-1, keepdims=True)
    if op_name == "min":
        return np.min(array, axis=-1, keepdims=True)
    raise KeyError(f"unknown operator: {op_name}")


def apply_chain(
    array: np.ndarray,
    op_chain: Sequence[str],
    axis_order: Sequence[int],
    eps: float,
) -> np.ndarray:
    result = array
    for op_name in op_chain:
        result = apply_op(result, op_name, axis_order, eps)
    return result
