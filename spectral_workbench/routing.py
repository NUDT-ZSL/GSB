"""Top-k gated routing with capacity constraints and load statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .utils import softmax_rows


@dataclass(frozen=True)
class RouterConfig:
    num_experts: int
    top_k: int
    temperature: float
    capacity_factor: float
    overflow_strategy: str
    near_tie_margin: float


@dataclass
class RouteResult:
    logits: np.ndarray
    scaled_logits: np.ndarray
    probabilities: np.ndarray
    topk_indices: np.ndarray
    pre_assignment: np.ndarray
    assignments: np.ndarray
    activation_weights: np.ndarray
    token_counts: np.ndarray
    sample_counts: np.ndarray
    utilization: np.ndarray
    capacity_per_expert: int
    active_token_count: int
    assignment_count: int
    overflow_events: int
    dropped_slots: int
    rerouted_slots: int
    tokens_without_assignment: int
    tokens_below_top_k: int
    near_tie_mask: np.ndarray
    balance_loss: float
    route_loss: float
    capacity_loss: float

    def summary(self) -> dict[str, Any]:
        return {
            "active_token_count": int(self.active_token_count),
            "assignment_count": int(self.assignment_count),
            "capacity_per_expert": int(self.capacity_per_expert),
            "token_counts": self.token_counts,
            "sample_counts": self.sample_counts,
            "utilization": self.utilization,
            "overflow_events": int(self.overflow_events),
            "dropped_slots": int(self.dropped_slots),
            "rerouted_slots": int(self.rerouted_slots),
            "tokens_without_assignment": int(self.tokens_without_assignment),
            "tokens_below_top_k": int(self.tokens_below_top_k),
            "near_tie_token_count": int(np.sum(self.near_tie_mask)),
            "balance_loss": float(self.balance_loss),
            "route_loss": float(self.route_loss),
            "capacity_loss": float(self.capacity_loss),
        }


class GatingNetwork:
    """A deterministic affine gate followed by temperature-scaled softmax."""

    def __init__(self, weight: np.ndarray, bias: np.ndarray) -> None:
        self.weight = np.asarray(weight, dtype=np.float64).copy()
        self.bias = np.asarray(bias, dtype=np.float64).copy()

    @classmethod
    def initialize(
        cls,
        token_dim: int,
        num_experts: int,
        seed: int,
        bias_init: float | Sequence[float] = 0.0,
    ) -> "GatingNetwork":
        rng = np.random.default_rng(seed)
        scale = 1.0 / np.sqrt(float(token_dim))
        weight = rng.normal(0.0, scale, size=(token_dim, num_experts))
        if np.isscalar(bias_init):
            bias = np.full(num_experts, float(bias_init), dtype=np.float64)
        else:
            bias = np.asarray(bias_init, dtype=np.float64)
            if bias.shape != (num_experts,):
                raise ValueError(
                    "gate bias initialization must be scalar or length num_experts"
                )
        return cls(weight, bias)

    def logits(self, tokens: np.ndarray) -> np.ndarray:
        return np.asarray(tokens, dtype=np.float64) @ self.weight + self.bias

    def snapshot(self) -> dict[str, np.ndarray]:
        return {"gate_weight": self.weight.copy(), "gate_bias": self.bias.copy()}

    def reset(
        self, seed: int, bias_init: float | Sequence[float] = 0.0
    ) -> None:
        fresh = self.initialize(
            self.weight.shape[0], self.weight.shape[1], seed, bias_init
        )
        self.weight[...] = fresh.weight
        self.bias[...] = fresh.bias


def _capacity_dispatch(
    probabilities: np.ndarray,
    topk_indices: np.ndarray,
    config: RouterConfig,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    token_count = probabilities.shape[0]
    num_experts = probabilities.shape[1]
    assignments = np.zeros_like(probabilities, dtype=np.float64)
    if token_count == 0:
        return (
            assignments,
            np.zeros((0, num_experts), dtype=np.float64),
            {
                "capacity_per_expert": 1,
                "assignment_count": 0,
                "overflow_events": 0,
                "dropped_slots": 0,
                "rerouted_slots": 0,
            },
        )

    capacity_per_expert = max(
        1,
        int(
            np.floor(
                float(config.capacity_factor)
                * token_count
                * int(config.top_k)
                / float(num_experts)
            )
        ),
    )
    counts = np.zeros(num_experts, dtype=np.int64)
    candidates: list[tuple[float, int, int, int]] = []
    for token_index in range(token_count):
        for rank, expert_index in enumerate(topk_indices[token_index]):
            candidates.append(
                (
                    -float(probabilities[token_index, expert_index]),
                    token_index,
                    rank,
                    int(expert_index),
                )
            )
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    overflow_events = 0
    dropped_slots = 0
    rerouted_slots = 0
    for _negative_probability, token_index, _rank, expert_index in candidates:
        if int(assignments[token_index].sum()) >= int(config.top_k):
            continue
        if assignments[token_index, expert_index] > 0.0:
            continue
        if counts[expert_index] < capacity_per_expert:
            assignments[token_index, expert_index] = 1.0
            counts[expert_index] += 1
            continue

        overflow_events += 1
        if config.overflow_strategy == "drop":
            dropped_slots += 1
            continue

        rerouted = False
        preference = np.argsort(-probabilities[token_index], kind="stable")
        for alternative in preference:
            alternative = int(alternative)
            if assignments[token_index, alternative] > 0.0:
                continue
            if counts[alternative] >= capacity_per_expert:
                continue
            assignments[token_index, alternative] = 1.0
            counts[alternative] += 1
            rerouted_slots += 1
            rerouted = True
            break
        if not rerouted:
            dropped_slots += 1

    weights = np.zeros_like(probabilities, dtype=np.float64)
    for token_index in range(token_count):
        selected = assignments[token_index].astype(bool)
        if not selected.any():
            continue
        selected_probabilities = probabilities[token_index, selected]
        total = float(selected_probabilities.sum())
        if total <= 1.0e-15:
            weights[token_index, selected] = 1.0 / float(selected.sum())
        else:
            weights[token_index, selected] = selected_probabilities / total
    return (
        assignments,
        weights,
        {
            "capacity_per_expert": capacity_per_expert,
            "assignment_count": int(counts.sum()),
            "overflow_events": overflow_events,
            "dropped_slots": dropped_slots,
            "rerouted_slots": rerouted_slots,
        },
    )


def route_tokens(
    network: GatingNetwork,
    tokens: np.ndarray,
    sample_ids: np.ndarray,
    config: RouterConfig,
) -> RouteResult:
    """Route valid (non-missing) tokens.

    ``tokens`` must have shape ``(token_count, token_dim)`` and ``sample_ids``
    must identify the source sample of each row. Missing tokens are filtered by
    the caller so they never participate in load statistics.
    """

    tokens = np.asarray(tokens, dtype=np.float64)
    sample_ids = np.asarray(sample_ids)
    token_count = int(tokens.shape[0])
    num_experts = int(config.num_experts)
    if tokens.ndim != 2:
        raise ValueError("tokens must be two-dimensional")
    if sample_ids.shape[0] != token_count:
        raise ValueError("sample_ids length must match tokens")
    if token_count == 0:
        zeros = np.zeros((0, num_experts), dtype=np.float64)
        return RouteResult(
            logits=zeros,
            scaled_logits=zeros,
            probabilities=zeros,
            topk_indices=np.zeros((0, config.top_k), dtype=np.int64),
            pre_assignment=zeros,
            assignments=zeros,
            activation_weights=zeros,
            token_counts=np.zeros(num_experts, dtype=np.int64),
            sample_counts=np.zeros(num_experts, dtype=np.int64),
            utilization=np.zeros(num_experts, dtype=np.float64),
            capacity_per_expert=1,
            active_token_count=0,
            assignment_count=0,
            overflow_events=0,
            dropped_slots=0,
            rerouted_slots=0,
            tokens_without_assignment=0,
            tokens_below_top_k=0,
            near_tie_mask=np.zeros(0, dtype=bool),
            balance_loss=0.0,
            route_loss=0.0,
            capacity_loss=0.0,
        )

    logits = network.logits(tokens)
    scaled_logits = logits / float(config.temperature)
    probabilities = softmax_rows(scaled_logits)
    topk_indices = np.argsort(-probabilities, axis=1, kind="stable")[
        :, : int(config.top_k)
    ]
    pre_assignment = np.zeros_like(probabilities, dtype=np.float64)
    for token_index in range(token_count):
        pre_assignment[token_index, topk_indices[token_index]] = 1.0

    assignments, weights, capacity_stats = _capacity_dispatch(
        probabilities, topk_indices, config
    )
    assignment_count = int(capacity_stats["assignment_count"])
    token_counts = assignments.sum(axis=0).astype(np.int64)
    if assignment_count > 0:
        utilization = token_counts.astype(np.float64) / float(assignment_count)
    else:
        utilization = np.zeros(num_experts, dtype=np.float64)

    sample_counts = np.zeros(num_experts, dtype=np.int64)
    for expert_index in range(num_experts):
        assigned = assignments[:, expert_index].astype(bool)
        if assigned.any():
            sample_counts[expert_index] = int(np.unique(sample_ids[assigned]).size)

    sorted_probabilities = np.sort(probabilities, axis=1)[:, ::-1]
    if int(config.top_k) < num_experts:
        margins = (
            sorted_probabilities[:, int(config.top_k) - 1]
            - sorted_probabilities[:, int(config.top_k)]
        )
    else:
        margins = sorted_probabilities[:, int(config.top_k) - 1]
    near_tie_mask = margins <= float(config.near_tie_margin)

    if assignment_count > 0:
        load_fraction = token_counts.astype(np.float64) / float(assignment_count)
        weight_sums = weights.sum(axis=0)
        if float(weight_sums.sum()) > 1.0e-15:
            gate_fraction = weight_sums / float(weight_sums.sum())
        else:
            gate_fraction = probabilities.mean(axis=0)
            gate_fraction = gate_fraction / float(gate_fraction.sum())
        balance_loss = float(num_experts * np.dot(load_fraction, gate_fraction))
    else:
        balance_loss = 0.0

    maximum = np.max(scaled_logits, axis=1, keepdims=True)
    logsumexp = np.squeeze(maximum, axis=1) + np.log(
        np.sum(np.exp(scaled_logits - maximum), axis=1)
    )
    route_loss = float(np.mean(logsumexp * logsumexp))
    capacity_loss = float(
        capacity_stats["overflow_events"] / max(1, token_count * int(config.top_k))
    )
    tokens_without_assignment = int(
        np.sum(assignments.sum(axis=1) <= 0.0)
    )
    tokens_below_top_k = int(
        np.sum(assignments.sum(axis=1) < float(config.top_k))
    )

    return RouteResult(
        logits=logits,
        scaled_logits=scaled_logits,
        probabilities=probabilities,
        topk_indices=topk_indices,
        pre_assignment=pre_assignment,
        assignments=assignments,
        activation_weights=weights,
        token_counts=token_counts,
        sample_counts=sample_counts,
        utilization=utilization,
        capacity_per_expert=int(capacity_stats["capacity_per_expert"]),
        active_token_count=token_count,
        assignment_count=assignment_count,
        overflow_events=int(capacity_stats["overflow_events"]),
        dropped_slots=int(capacity_stats["dropped_slots"]),
        rerouted_slots=int(capacity_stats["rerouted_slots"]),
        tokens_without_assignment=tokens_without_assignment,
        tokens_below_top_k=tokens_below_top_k,
        near_tie_mask=near_tie_mask,
        balance_loss=balance_loss,
        route_loss=route_loss,
        capacity_loss=capacity_loss,
    )
