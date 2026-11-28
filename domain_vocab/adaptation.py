"""NumPy-only frozen-embedding adaptation and downstream evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .extension import ExtensionBuild
from .generator import GeneratedData, LabeledSample


@dataclass
class TokenizedSample:
    sample: LabeledSample
    token_ids: list[int]
    is_single_new_token: bool


@dataclass
class AdapterParameters:
    weight: np.ndarray
    bias: np.ndarray


def initial_adapter(dim: int) -> AdapterParameters:
    return AdapterParameters(
        weight=np.eye(dim, dtype=np.float64),
        bias=np.zeros(dim, dtype=np.float64),
    )


def tokenize_sample(
    sample: LabeledSample,
    build: ExtensionBuild,
    base_token_to_id: dict[str, int],
    use_new_tokens: bool,
) -> TokenizedSample:
    if use_new_tokens and sample.entity_key in build.entity_to_token_id:
        return TokenizedSample(
            sample=sample,
            token_ids=[build.entity_to_token_id[sample.entity_key]],
            is_single_new_token=True,
        )

    fallback = build.fallback_tokens_by_entity.get(sample.entity_key, ["<unk>"])
    token_ids = [base_token_to_id.get(token, base_token_to_id["<unk>"]) for token in fallback]
    return TokenizedSample(
        sample=sample,
        token_ids=token_ids,
        is_single_new_token=False,
    )


def _pool_embeddings(embeddings: np.ndarray, token_ids: list[int]) -> np.ndarray:
    if not token_ids:
        raise ValueError("token_ids must not be empty")
    return embeddings[np.asarray(token_ids, dtype=np.int64)].mean(axis=0)


def _logits(
    embeddings: np.ndarray,
    tokenized_samples: Iterable[TokenizedSample],
    adapter: AdapterParameters,
    head: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    samples = list(tokenized_samples)
    pooled = np.stack(
        [_pool_embeddings(embeddings, item.token_ids) for item in samples], axis=0
    )
    hidden = pooled @ adapter.weight + adapter.bias
    return pooled, hidden @ head


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / exponentials.sum(axis=1, keepdims=True)


def cross_entropy_and_gradient(
    embeddings: np.ndarray,
    tokenized_samples: list[TokenizedSample],
    adapter: AdapterParameters,
    head: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return loss plus gradients for embeddings, W, b and hidden states."""
    pooled, logits = _logits(embeddings, tokenized_samples, adapter, head)
    labels = np.asarray([item.sample.label for item in tokenized_samples], dtype=np.int64)
    probabilities = _softmax(logits)
    count = len(tokenized_samples)
    loss = float(-np.log(np.maximum(probabilities[np.arange(count), labels], 1e-12)).mean())

    gradient_logits = probabilities
    gradient_logits[np.arange(count), labels] -= 1.0
    gradient_logits /= count
    gradient_hidden = gradient_logits @ head.T
    gradient_weight = pooled.T @ gradient_hidden
    gradient_bias = gradient_hidden.sum(axis=0)
    gradient_pooled = gradient_hidden @ adapter.weight.T

    gradient_embeddings = np.zeros_like(embeddings)
    for row, item in enumerate(tokenized_samples):
        contribution = gradient_pooled[row] / len(item.token_ids)
        for token_id in item.token_ids:
            gradient_embeddings[token_id] += contribution
    return loss, gradient_embeddings, gradient_weight, gradient_bias, gradient_pooled


@dataclass
class AdamState:
    first: np.ndarray
    second: np.ndarray


def _adam_update(
    value: np.ndarray,
    gradient: np.ndarray,
    state: AdamState,
    step: int,
    learning_rate: float,
    beta1: float = 0.9,
    beta2: float = 0.999,
    epsilon: float = 1e-8,
) -> None:
    state.first = beta1 * state.first + (1.0 - beta1) * gradient
    state.second = beta2 * state.second + (1.0 - beta2) * (gradient * gradient)
    first_hat = state.first / (1.0 - beta1**step)
    second_hat = state.second / (1.0 - beta2**step)
    value -= learning_rate * first_hat / (np.sqrt(second_hat) + epsilon)


def _clip_gradient(gradient: np.ndarray, max_norm: float = 5.0) -> np.ndarray:
    norm = float(np.linalg.norm(gradient))
    if norm > max_norm:
        return gradient * (max_norm / norm)
    return gradient


def train_adapter(
    data: GeneratedData,
    build: ExtensionBuild,
    epochs: int,
    learning_rate: float,
) -> dict[str, object]:
    if epochs < 1:
        raise ValueError("epochs must be positive")
    if learning_rate <= 0:
        raise ValueError("learning_rate must be positive")

    base_size = len(data.base_vocab)
    embeddings = build.embeddings.copy()
    initial_new_embeddings = embeddings[base_size:].copy()
    original_snapshot = embeddings[:base_size].copy()
    adapter = initial_adapter(embeddings.shape[1])
    head = data.fixed_head
    base_token_to_id = {token: index for index, token in enumerate(data.base_vocab)}
    train_samples = [
        tokenize_sample(sample, build, base_token_to_id, use_new_tokens=True)
        for sample in data.labeled_samples
        if sample.split == "train"
    ]
    if not train_samples:
        raise ValueError("no training samples were generated")

    embedding_state = AdamState(
        first=np.zeros_like(embeddings[base_size:]),
        second=np.zeros_like(embeddings[base_size:]),
    )
    weight_state = AdamState(
        first=np.zeros_like(adapter.weight), second=np.zeros_like(adapter.weight)
    )
    bias_state = AdamState(first=np.zeros_like(adapter.bias), second=np.zeros_like(adapter.bias))

    epoch_losses: list[float] = []
    frozen_checks: list[bool] = []
    for epoch in range(1, epochs + 1):
        loss, grad_embeddings, grad_weight, grad_bias, _ = cross_entropy_and_gradient(
            embeddings, train_samples, adapter, head
        )
        epoch_losses.append(loss)
        grad_embeddings = _clip_gradient(grad_embeddings)
        grad_weight = _clip_gradient(grad_weight)
        grad_bias = _clip_gradient(grad_bias)

        _adam_update(
            embeddings[base_size:],
            grad_embeddings[base_size:],
            embedding_state,
            epoch,
            learning_rate,
        )
        _adam_update(
            adapter.weight, grad_weight, weight_state, epoch, learning_rate
        )
        _adam_update(adapter.bias, grad_bias, bias_state, epoch, learning_rate)
        frozen_checks.append(np.array_equal(embeddings[:base_size], original_snapshot))

    update_per_token = np.linalg.norm(embeddings[base_size:] - initial_new_embeddings, axis=1)
    original_max_abs_change = float(
        np.max(np.abs(embeddings[:base_size] - original_snapshot))
    )
    update_stats = {
        "per_token_update_l2_norm": {
            candidate.whole_token: float(update_per_token[index])
            for index, candidate in enumerate(build.accepted)
        },
        "minimum": float(update_per_token.min()) if update_per_token.size else 0.0,
        "maximum": float(update_per_token.max()) if update_per_token.size else 0.0,
        "mean": float(update_per_token.mean()) if update_per_token.size else 0.0,
        "median": float(np.median(update_per_token)) if update_per_token.size else 0.0,
    }
    dim = embeddings.shape[1]
    total_rows = embeddings.shape[0]
    parameter_ranges = {
        "embedding_rows_half_open": [base_size, total_rows],
        "embedding_rows_inclusive": [base_size, total_rows - 1],
        "embedding_flat_half_open": [base_size * dim, total_rows * dim],
        "adapter_weight_flat_half_open": [0, dim * dim],
        "adapter_bias_flat_half_open": [dim * dim, dim * dim + dim],
        "all_trainable_flat_half_open": [base_size * dim, total_rows * dim + dim * dim + dim],
        "frozen_embedding_rows_half_open": [0, base_size],
    }
    return {
        "epochs": epochs,
        "learning_rate": learning_rate,
        "train_sample_count": len(train_samples),
        "epoch_losses": epoch_losses,
        "initial_loss": epoch_losses[0],
        "final_loss": epoch_losses[-1],
        "adapter_weight_update_l2_norm": float(
            np.linalg.norm(adapter.weight - np.eye(dim))
        ),
        "adapter_bias_update_l2_norm": float(np.linalg.norm(adapter.bias)),
        "parameter_index_ranges": parameter_ranges,
        "new_token_embedding_update_stats": update_stats,
        "frozen_verification": {
            "original_rows_unchanged": bool(all(frozen_checks))
            and original_max_abs_change == 0.0,
            "checked_every_epoch": True,
            "epoch_check_count": len(frozen_checks),
            "max_abs_change": original_max_abs_change,
            "elementwise_equal": original_max_abs_change == 0.0,
        },
        "adapted_embeddings": embeddings,
        "adapted_adapter": adapter,
    }


def _losses(
    embeddings: np.ndarray,
    tokenized_samples: list[TokenizedSample],
    adapter: AdapterParameters,
    head: np.ndarray,
) -> np.ndarray:
    pooled, logits = _logits(embeddings, tokenized_samples, adapter, head)
    probabilities = _softmax(logits)
    labels = np.asarray([item.sample.label for item in tokenized_samples], dtype=np.int64)
    del pooled
    return -np.log(np.maximum(probabilities[np.arange(len(labels)), labels], 1e-12))


def evaluate_downstream(
    data: GeneratedData,
    build: ExtensionBuild,
    trained: dict[str, object],
    uncertain_tolerance: float = 1e-3,
) -> dict[str, object]:
    base_token_to_id = {token: index for index, token in enumerate(data.base_vocab)}
    eval_samples = [
        sample for sample in data.labeled_samples if sample.split == "eval"
    ]
    if not eval_samples:
        eval_samples = list(data.labeled_samples)

    before_tokenized = [
        tokenize_sample(sample, build, base_token_to_id, use_new_tokens=False)
        for sample in eval_samples
    ]
    after_tokenized = [
        tokenize_sample(sample, build, base_token_to_id, use_new_tokens=True)
        for sample in eval_samples
    ]

    before_embeddings = data.base_embeddings
    before_adapter = initial_adapter(data.base_embeddings.shape[1])
    after_embeddings = trained["adapted_embeddings"]
    after_adapter = trained["adapted_adapter"]
    assert isinstance(after_embeddings, np.ndarray)
    assert isinstance(after_adapter, AdapterParameters)

    before_losses = _losses(before_embeddings, before_tokenized, before_adapter, data.fixed_head)
    after_losses = _losses(after_embeddings, after_tokenized, after_adapter, data.fixed_head)
    before_hits = np.zeros(len(eval_samples), dtype=np.float64)
    after_hits = np.asarray(
        [1.0 if item.is_single_new_token else 0.0 for item in after_tokenized],
        dtype=np.float64,
    )

    grouped_indices: dict[str, list[int]] = {}
    for index, sample in enumerate(eval_samples):
        grouped_indices.setdefault(sample.entity_key, []).append(index)

    details: list[dict[str, object]] = []
    uncertain: list[dict[str, object]] = []
    low_support: list[dict[str, object]] = []
    for entity_key in sorted(grouped_indices):
        indices = grouped_indices[entity_key]
        entity = next(entity for entity in data.entities if entity.key == entity_key)
        candidate = next(
            candidate
            for candidate in build.candidates
            if candidate.entity.key == entity_key
        )
        loss_before = float(before_losses[indices].mean())
        loss_after = float(after_losses[indices].mean())
        hit_before = float(before_hits[indices].mean())
        hit_after = float(after_hits[indices].mean())
        loss_gain = loss_before - loss_after
        support = len(indices)
        sample_gains = before_losses[indices] - after_losses[indices]
        positive_count = int(np.sum(sample_gains > uncertain_tolerance))
        negative_count = int(np.sum(sample_gains < -uncertain_tolerance))
        neutral_count = support - positive_count - negative_count
        mixed_direction = positive_count > 0 and negative_count > 0
        is_uncertain = (
            support < 2
            or mixed_direction
            or abs(loss_gain) <= uncertain_tolerance
        )
        detail = {
            "identifier_key": entity_key,
            "canonical_id": entity.canonical_id,
            "whole_token": candidate.whole_token,
            "accepted_as_new_token": candidate.accepted,
            "support": support,
            "loss_before": loss_before,
            "loss_after": loss_after,
            "loss_gain": loss_gain,
            "hit_rate_before": hit_before,
            "hit_rate_after": hit_after,
            "hit_rate_gain": hit_after - hit_before,
            "gain_direction": "positive" if loss_gain > 0 else (
                "negative" if loss_gain < 0 else "neutral"
            ),
            "sample_gain_positive_count": positive_count,
            "sample_gain_negative_count": negative_count,
            "sample_gain_neutral_count": neutral_count,
            "low_support": support < 2,
            "direction_uncertain": is_uncertain,
        }
        details.append(detail)
        if is_uncertain:
            uncertain.append(
                {
                    "identifier_key": entity_key,
                    "canonical_id": entity.canonical_id,
                    "support": support,
                    "loss_gain": loss_gain,
                    "reason": (
                        "留出样本少于 2 条，方向置信度不足"
                        if support < 2
                        else (
                            "逐样本损失增益方向不一致"
                            if mixed_direction
                            else "按标识聚合的损失增益接近零"
                        )
                    ),
                }
            )
        if support < 2:
            low_support.append(
                {
                    "identifier_key": entity_key,
                    "canonical_id": entity.canonical_id,
                    "support": support,
                    "loss_gain": loss_gain,
                    "note": "留出样本少于 2 条，统计置信度有限",
                }
            )

    loss_before = float(before_losses.mean())
    loss_after = float(after_losses.mean())
    hit_before = float(before_hits.mean())
    hit_after = float(after_hits.mean())
    loss_gain = loss_before - loss_after
    return {
        "eval_sample_count": len(eval_samples),
        "loss_before": loss_before,
        "loss_after": loss_after,
        "loss_gain": loss_gain,
        "loss_reduction_pct": (loss_gain / loss_before * 100.0) if loss_before else 0.0,
        "loss_direction": "improved" if loss_gain > 0 else (
            "degraded" if loss_gain < 0 else "neutral"
        ),
        "hit_rate_before": hit_before,
        "hit_rate_after": hit_after,
        "hit_rate_gain": hit_after - hit_before,
        "identifier_details": details,
        "uncertain_identifiers": uncertain,
        "low_support_identifiers": low_support,
        "uncertain_tolerance": uncertain_tolerance,
    }

