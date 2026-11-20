"""Activation and transpose primitives, plus one-shot reference execution."""

import numpy as np

from .errors import ErrorCode, OpChainError
from .task import ACTIVATIONS, TRANSPOSE


def activation_forward(kind, tensor):
    if kind == "relu":
        return np.maximum(tensor, 0.0)
    if kind == "gelu":
        # tanh-based GELU approximation (deterministic, elementwise).
        return 0.5 * tensor * (
            1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (tensor + 0.044715 * tensor**3))
        )
    if kind == "sigmoid":
        # Branch-stable logistic without platform-dependent exp overflow.
        positive = tensor >= 0
        output = np.empty_like(tensor, dtype=np.float64)
        output[positive] = 1.0 / (1.0 + np.exp(-tensor[positive]))
        exp_value = np.exp(tensor[~positive])
        output[~positive] = exp_value / (1.0 + exp_value)
        if tensor.dtype != np.float64:
            output = output.astype(tensor.dtype)
        return output
    raise OpChainError(
        ErrorCode.OP_UNKNOWN, f"unknown activation kind {kind!r}", {"kind": kind}
    )


def transpose_contiguous(tensor, axes):
    """Permute semantic axes and materialize a C-contiguous view."""
    permuted = np.transpose(tensor, axes)
    return np.ascontiguousarray(permuted)


def run_op(op, tensor, default_activation):
    name = op["op"]
    if name in ACTIVATIONS:
        kind = op.get("kind", default_activation)
        return activation_forward(kind, tensor)
    if name == TRANSPOSE:
        return transpose_contiguous(tensor, op["axes"])
    raise OpChainError(
        ErrorCode.OP_UNKNOWN, f"unknown operator {name!r}", {"op": op}
    )


def run_reference_chain(input_tensor, spec):
    """One-shot whole-segment reference path in float64."""
    tensor = np.ascontiguousarray(input_tensor.astype(np.float64))
    stages = []
    for stage, op in enumerate(spec.op_chain):
        tensor = run_op(op, tensor, spec.activation_kind)
        stages.append(
            {
                "stage": stage,
                "op": op["op"],
                "array": np.ascontiguousarray(tensor),
            }
        )
    return tensor, stages
