"""One deterministic operator chain shared by training and inference modes."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .configs import ResolvedConfig
from .errors import EConfigInvalid
from .models import Task
from .util import stable_u64


OPERATOR_CONTRACT = {
    "forward_contract": "tanh-linear-colchunk-v1",
    "reduction_order": "row_block_then_feature_col_chunk",
    "activation": "tanh",
    "accumulation_dtype": "float64",
    "weight_layout": "feature_dim_x_label_dim",
}


def build_model(task: Task, config: ResolvedConfig) -> dict[str, Any]:
    if config.hyperparameters.get("activation") != "tanh":
        raise EConfigInvalid(
            "only tanh activation is supported by this operator contract",
            {"activation": config.hyperparameters.get("activation")},
        )
    if config.hyperparameters.get("rounding") != "half_away_from_zero":
        raise EConfigInvalid(
            "unsupported rounding contract",
            {"rounding": config.hyperparameters.get("rounding")},
        )
    seed = stable_u64(
        "model",
        config.generation,
        config.config_hash,
        task.feature_dim,
        task.label_dim,
        config.hyperparameters.get("seed_offset", 0),
    )
    generator = np.random.Generator(np.random.PCG64(seed))
    weight_scale = float(config.hyperparameters.get("weight_scale", 0.8))
    weights = generator.normal(
        loc=0.0,
        scale=weight_scale / np.sqrt(task.feature_dim),
        size=(task.feature_dim, task.label_dim),
    ).astype(np.float64)
    bias = generator.normal(
        loc=0.0,
        scale=0.01,
        size=(task.label_dim,),
    ).astype(np.float64)
    digest = hashlib.sha256()
    digest.update(weights.tobytes(order="C"))
    digest.update(bias.tobytes(order="C"))
    return {
        "weights": weights,
        "bias": bias,
        "summary": {
            "generation": config.generation,
            "config_hash": config.config_hash,
            "weight_shape": list(weights.shape),
            "bias_shape": list(bias.shape),
            "model_digest": digest.hexdigest(),
            "operator_contract": OPERATOR_CONTRACT,
        },
    }


def _block_boundaries(length: int, block: int) -> list[list[int]]:
    return [[start, min(start + block, length)] for start in range(0, length, block)]


def forward_chain(
    dequantized: np.ndarray,
    task: Task,
    model: dict[str, Any],
    config: ResolvedConfig,
) -> dict[str, Any]:
    """Execute the common forward chain with explicitly fixed block order."""

    expected_shape = (task.data_size, task.feature_dim)
    if dequantized.shape != expected_shape:
        raise ValueError(f"dequantized shape {dequantized.shape} != {expected_shape}")
    weights = model["weights"]
    bias = model["bias"]
    if weights.shape != (task.feature_dim, task.label_dim):
        raise ValueError("model weight shape does not match task dimensions")

    row_boundaries = _block_boundaries(task.data_size, task.row_block)
    col_boundaries = _block_boundaries(task.feature_dim, task.col_block)
    logits = np.empty((task.data_size, task.label_dim), dtype=np.float64)
    activations = np.empty((task.data_size, task.label_dim), dtype=np.float64)

    for row_start, row_end in row_boundaries:
        x_block = dequantized[row_start:row_end, :]
        linear = np.broadcast_to(bias, (row_end - row_start, task.label_dim)).copy()
        for col_start, col_end in col_boundaries:
            linear += x_block[:, col_start:col_end] @ weights[col_start:col_end, :]
        logits[row_start:row_end, :] = linear
        activations[row_start:row_end, :] = np.tanh(linear)

    block_digest = hashlib.sha256()
    block_digest.update(weights.tobytes(order="C"))
    block_digest.update(dequantized.tobytes(order="C"))
    block_digest.update(np.asarray(row_boundaries, dtype=np.int64).tobytes())
    block_digest.update(np.asarray(col_boundaries, dtype=np.int64).tobytes())
    block_digest.update(logits.tobytes(order="C"))
    block_digest.update(activations.tobytes(order="C"))
    block_digest.update(
        f"{config.config_hash}|{OPERATOR_CONTRACT['forward_contract']}".encode("ascii")
    )
    return {
        "logits": logits,
        "activations": activations,
        "row_boundaries": row_boundaries,
        "col_boundaries": col_boundaries,
        "summary": {
            "case_id": task.case_id,
            "forward_digest": block_digest.hexdigest(),
            "row_boundaries": row_boundaries,
            "col_boundaries": col_boundaries,
            "block_plan": dict(task.block_plan),
            "config_generation": config.generation,
            "config_hash": config.config_hash,
            "operator_contract": OPERATOR_CONTRACT,
        },
    }


def train_backward_chain(
    forward: dict[str, Any],
    sample: dict[str, Any],
    task: Task,
    config: ResolvedConfig,
) -> dict[str, Any]:
    """Train-mode suffix: MSE gradient with fixed row/col reduction order."""

    activations = forward["activations"]
    labels = sample["labels"]
    features = sample["dequantized"]
    denominator = float(task.data_size * task.label_dim)
    delta = 2.0 * (activations - labels) / denominator
    delta_logits = delta * (1.0 - activations * activations)
    grad_weights = np.zeros((task.feature_dim, task.label_dim), dtype=np.float64)
    grad_bias = np.zeros((task.label_dim,), dtype=np.float64)

    row_boundaries = _block_boundaries(task.data_size, task.row_block)
    col_boundaries = _block_boundaries(task.feature_dim, task.col_block)
    for row_start, row_end in row_boundaries:
        delta_block = delta_logits[row_start:row_end, :]
        grad_bias += np.sum(delta_block, axis=0)
        x_block = features[row_start:row_end, :]
        for col_start, col_end in col_boundaries:
            grad_weights[col_start:col_end, :] += (
                x_block[:, col_start:col_end].T @ delta_block
            )

    loss = float(np.sum((activations - labels) ** 2) / denominator)
    digest = hashlib.sha256()
    digest.update(grad_weights.tobytes(order="C"))
    digest.update(grad_bias.tobytes(order="C"))
    digest.update(f"{loss:.17g}".encode("ascii"))
    learning_rate = float(config.hyperparameters.get("learning_rate", 0.01))
    return {
        "grad_weights": grad_weights,
        "grad_bias": grad_bias,
        "summary": {
            "case_id": task.case_id,
            "mode": "train",
            "loss": loss,
            "gradient_digest": digest.hexdigest(),
            "learning_rate": learning_rate,
            "learning_rate_note": "gradients are raw; optimizer applies learning_rate",
            "reduction_order": "row_block_then_feature_col_chunk",
            "forward_digest": forward["summary"]["forward_digest"],
        },
    }


def inference_chain(
    forward: dict[str, Any],
    task: Task,
    config: ResolvedConfig,
) -> dict[str, Any]:
    """Inference suffix: deterministic argmax and per-row softmax confidence."""

    activations = forward["activations"]
    predictions = np.argmax(activations, axis=1).astype(np.int64)
    shifted = activations - np.max(activations, axis=1, keepdims=True)
    exponentiated = np.exp(shifted)
    probabilities = exponentiated / np.sum(exponentiated, axis=1, keepdims=True)
    confidence = np.max(probabilities, axis=1)
    digest = hashlib.sha256()
    digest.update(predictions.tobytes(order="C"))
    digest.update(probabilities.tobytes(order="C"))
    return {
        "predictions": predictions,
        "probabilities": probabilities,
        "confidence": confidence,
        "summary": {
            "case_id": task.case_id,
            "mode": "infer",
            "prediction_digest": digest.hexdigest(),
            "forward_digest": forward["summary"]["forward_digest"],
            "prediction_contract": "argmax_tanh_logits",
            "config_generation": config.generation,
        },
    }


def execute_operator_chain(
    *,
    task: Task,
    config: ResolvedConfig,
    sample: dict[str, Any],
    dequantized: np.ndarray,
    mode: str,
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single mode-aware entry point used by both train and inference paths.

    The forward operator order, row/col block boundaries, and reduction order
    are identical for every mode; only the suffix differs.
    """

    if mode not in {"train", "infer", "both"}:
        raise ValueError(f"unknown execution mode: {mode}")
    model = model or build_model(task, config)
    forward = forward_chain(dequantized, task, model, config)
    result: dict[str, Any] = {
        "mode": mode,
        "forward": forward,
        "model": model,
        "block_boundaries_shared": True,
        "operator_contract": OPERATOR_CONTRACT,
    }
    if mode in {"train", "both"}:
        train_sample = {
            "labels": sample["labels"],
            "dequantized": dequantized,
        }
        result["train"] = train_backward_chain(forward, train_sample, task, config)
    if mode in {"infer", "both"}:
        result["inference"] = inference_chain(forward, task, config)
    return result
