"""A small deterministic MLP with exact SGD + momentum updates."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from .canonical import digest_array_map
from .config import TrainingConfig
from .data import NUM_CLASSES


PARAMETER_NAMES = ("W1", "b1", "W2", "b2")
INIT_SEED_TAG = 0x1A11CE


def parameter_shapes(config: TrainingConfig) -> dict[str, tuple[int, ...]]:
    return {
        "W1": (config.hidden_dim, config.features),
        "b1": (config.hidden_dim,),
        "W2": (NUM_CLASSES, config.hidden_dim),
        "b2": (NUM_CLASSES,),
    }


def initial_parameters(config: TrainingConfig) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(
        [config.seed, INIT_SEED_TAG, config.features, config.hidden_dim]
    )
    shapes = parameter_shapes(config)
    scale1 = float(np.sqrt(2.0 / config.features))
    scale2 = float(np.sqrt(2.0 / config.hidden_dim))
    values = {
        "W1": rng.standard_normal(shapes["W1"]) * scale1,
        "b1": np.zeros(shapes["b1"], dtype=np.float64),
        "W2": rng.standard_normal(shapes["W2"]) * scale2,
        "b2": np.zeros(shapes["b2"], dtype=np.float64),
    }
    return {
        name: np.asarray(values[name], dtype=np.dtype("<f8"))
        for name in PARAMETER_NAMES
    }


def initial_momentum(config: TrainingConfig) -> dict[str, np.ndarray]:
    return {
        name: np.zeros(parameter_shapes(config)[name], dtype=np.dtype("<f8"))
        for name in PARAMETER_NAMES
    }


def initial_parameter_digest(config: TrainingConfig) -> str:
    return digest_array_map(initial_parameters(config))


def validate_parameter_map(
    parameters: Mapping[str, np.ndarray], config: TrainingConfig
) -> None:
    expected = parameter_shapes(config)
    if set(parameters) != set(expected):
        raise ValueError(
            f"parameter names mismatch: {sorted(parameters)} != {sorted(expected)}"
        )
    for name, shape in expected.items():
        value = np.asarray(parameters[name])
        if value.shape != shape or value.dtype.kind != "f" or value.dtype.itemsize != 8:
            raise ValueError(
                f"parameter {name} has shape/dtype {value.shape}/{value.dtype}, "
                f"expected {shape}/float64"
            )


def forward(
    parameters: Mapping[str, np.ndarray],
    features: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    preact = features @ parameters["W1"].T + parameters["b1"]
    hidden = np.maximum(preact, 0.0)
    logits = hidden @ parameters["W2"].T + parameters["b2"]
    return preact, hidden, logits


def cross_entropy_loss(
    parameters: Mapping[str, np.ndarray],
    features: np.ndarray,
    labels: np.ndarray,
) -> float:
    _, _, logits = forward(parameters, features)
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    log_probs = shifted - np.log(np.sum(np.exp(shifted), axis=1, keepdims=True))
    selected = log_probs[np.arange(labels.shape[0]), labels]
    return float(-np.mean(selected))


def gradients_for_batch(
    parameters: Mapping[str, np.ndarray],
    features: np.ndarray,
    labels: np.ndarray,
) -> dict[str, np.ndarray]:
    preact, hidden, logits = forward(parameters, features)
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    exp_logits = np.exp(shifted)
    probabilities = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)
    one_hot = np.zeros_like(probabilities)
    one_hot[np.arange(labels.shape[0]), labels] = 1.0
    dlogits = (probabilities - one_hot) / float(labels.shape[0])

    grads: dict[str, np.ndarray] = {}
    grads["W2"] = np.asarray(dlogits.T @ hidden, dtype=np.float64)
    grads["b2"] = np.asarray(np.sum(dlogits, axis=0), dtype=np.float64)
    dhidden = dlogits @ parameters["W2"]
    dpreact = dhidden.copy()
    dpreact[preact <= 0.0] = 0.0
    grads["W1"] = np.asarray(dpreact.T @ features, dtype=np.float64)
    grads["b1"] = np.asarray(np.sum(dpreact, axis=0), dtype=np.float64)
    return {name: grads[name] for name in PARAMETER_NAMES}


def apply_sgd_with_momentum(
    parameters: Mapping[str, np.ndarray],
    momentum: Mapping[str, np.ndarray],
    averaged_gradients: Mapping[str, np.ndarray],
    learning_rate: float,
    beta: float,
) -> None:
    for name in PARAMETER_NAMES:
        velocity = beta * momentum[name] + averaged_gradients[name]
        momentum[name][...] = velocity
        parameters[name][...] = parameters[name] - learning_rate * velocity
