"""NumPy gating network training with capacity-aware expert load accounting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .routing import GatingNetwork, RouteResult, RouterConfig, route_tokens
from .utils import append_jsonl, ensure_dir, array_sha256, write_json


def _logsumexp_rows(values: np.ndarray) -> np.ndarray:
    maximum = np.max(values, axis=1, keepdims=True)
    return np.squeeze(maximum, axis=1) + np.log(
        np.sum(np.exp(values - maximum), axis=1)
    )


def _clip_gradient(gradient: np.ndarray, limit: float = 2.0) -> np.ndarray:
    return np.clip(gradient, -limit, limit)


@dataclass
class ModelParameters:
    gate: GatingNetwork
    expert_weight: np.ndarray
    expert_bias: np.ndarray
    temperature: float


@dataclass
class LossBreakdown:
    task_loss: float
    balance_loss: float
    route_loss: float
    capacity_loss: float
    total_loss: float

    def as_dict(self) -> dict[str, float]:
        return {
            "task_loss": float(self.task_loss),
            "balance_loss": float(self.balance_loss),
            "route_loss": float(self.route_loss),
            "capacity_loss": float(self.capacity_loss),
            "total_loss": float(self.total_loss),
        }


@dataclass
class BatchEvaluation:
    route: RouteResult
    full_assignments: np.ndarray
    full_weights: np.ndarray
    full_probabilities: np.ndarray
    full_pre_assignments: np.ndarray
    loss: LossBreakdown


class AdamOptimizer:
    """Minimal deterministic Adam state for the gate and expert parameters."""

    def __init__(
        self,
        learning_rate: float,
        weight_decay: float,
        beta1: float = 0.9,
        beta2: float = 0.999,
        epsilon: float = 1.0e-8,
    ) -> None:
        self.learning_rate = float(learning_rate)
        self.weight_decay = float(weight_decay)
        self.beta1 = float(beta1)
        self.beta2 = float(beta2)
        self.epsilon = float(epsilon)
        self.first_moment: dict[str, np.ndarray] = {}
        self.second_moment: dict[str, np.ndarray] = {}
        self.steps: dict[str, int] = {}

    def step(self, key: str, parameter: np.ndarray, gradient: np.ndarray) -> None:
        if key not in self.first_moment:
            self.first_moment[key] = np.zeros_like(parameter)
            self.second_moment[key] = np.zeros_like(parameter)
            self.steps[key] = 0
        self.steps[key] += 1
        self.first_moment[key] = (
            self.beta1 * self.first_moment[key] + (1.0 - self.beta1) * gradient
        )
        self.second_moment[key] = (
            self.beta2 * self.second_moment[key]
            + (1.0 - self.beta2) * (gradient * gradient)
        )
        first_unbiased = self.first_moment[key] / (
            1.0 - self.beta1 ** self.steps[key]
        )
        second_unbiased = self.second_moment[key] / (
            1.0 - self.beta2 ** self.steps[key]
        )
        update = first_unbiased / (np.sqrt(second_unbiased) + self.epsilon)
        parameter -= self.learning_rate * (
            update + self.weight_decay * parameter
        )

    def reset(self, prefixes: tuple[str, ...]) -> None:
        for state in (self.first_moment, self.second_moment, self.steps):
            for key in list(state):
                if key in prefixes or key.startswith(prefixes):
                    del state[key]


def initialize_model(seed: int, config: dict[str, Any], token_dim: int) -> ModelParameters:
    model_config = config["model"]
    num_experts = int(model_config["num_experts"])
    gate = GatingNetwork.initialize(
        token_dim,
        num_experts,
        seed,
        model_config["gate_bias_init"],
    )
    rng = np.random.default_rng(seed + 17)
    identity_scale = float(model_config["expert_identity_scale"])
    expert_weight = (
        identity_scale * np.eye(token_dim, dtype=np.float64)[None, :, :]
        + rng.normal(
            0.0,
            0.35 / np.sqrt(float(token_dim)),
            size=(num_experts, token_dim, token_dim),
        )
    )
    expert_bias = rng.normal(
        0.0, 0.01, size=(num_experts, token_dim)
    )
    return ModelParameters(
        gate=gate,
        expert_weight=expert_weight,
        expert_bias=expert_bias,
        temperature=float(model_config["initial_temperature"]),
    )


def _router_config(config: dict[str, Any], temperature: float) -> RouterConfig:
    model_config = config["model"]
    return RouterConfig(
        num_experts=int(model_config["num_experts"]),
        top_k=int(model_config["top_k"]),
        temperature=float(temperature),
        capacity_factor=float(model_config["capacity_factor"]),
        overflow_strategy=str(model_config["overflow_strategy"]),
        near_tie_margin=float(model_config["near_tie_margin"]),
    )


def _flatten_batch(
    tokens: np.ndarray, masks: np.ndarray, sample_indices: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    batch_size, token_length, token_dim = tokens.shape
    valid = masks.reshape(batch_size * token_length).astype(bool)
    flattened = tokens.reshape(batch_size * token_length, token_dim)
    active_tokens = flattened[valid]
    token_sample_indices = np.repeat(sample_indices, token_length)[valid]
    return active_tokens, token_sample_indices, valid


def _expand_active_matrix(
    active_matrix: np.ndarray, valid: np.ndarray, batch_shape: tuple[int, int]
) -> np.ndarray:
    if active_matrix.ndim != 2:
        raise ValueError("active matrix must be two-dimensional")
    full = np.zeros(
        (batch_shape[0] * batch_shape[1], active_matrix.shape[1]),
        dtype=active_matrix.dtype,
    )
    full[valid] = active_matrix
    return full.reshape(batch_shape[0], batch_shape[1], active_matrix.shape[1])


def _reconstruction(
    tokens: np.ndarray,
    weights: np.ndarray,
    expert_weight: np.ndarray,
    expert_bias: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    num_experts = expert_weight.shape[0]
    expert_outputs = np.empty(
        (num_experts, tokens.shape[0], tokens.shape[1]), dtype=np.float64
    )
    reconstruction = np.zeros_like(tokens)
    for expert_index in range(num_experts):
        output = (
            tokens @ expert_weight[expert_index].T + expert_bias[expert_index]
        )
        expert_outputs[expert_index] = output
        reconstruction += weights[:, expert_index, None] * output
    return reconstruction, expert_outputs


def _loss_and_gradients(
    model: ModelParameters,
    config: dict[str, Any],
    tokens: np.ndarray,
    assignments: np.ndarray,
    weights: np.ndarray,
    probabilities: np.ndarray,
    route: RouteResult,
) -> tuple[LossBreakdown, dict[str, np.ndarray]]:
    model_config = config["model"]
    num_experts = int(model_config["num_experts"])
    token_count = tokens.shape[0]
    token_dim = tokens.shape[1]
    if token_count == 0:
        zeros_gate = np.zeros_like(model.gate.weight)
        zeros_bias = np.zeros_like(model.gate.bias)
        return (
            LossBreakdown(0.0, 0.0, 0.0, 0.0, 0.0),
            {
                "gate_weight": zeros_gate,
                "gate_bias": zeros_bias,
                "expert_weight": np.zeros_like(model.expert_weight),
                "expert_bias": np.zeros_like(model.expert_bias),
            },
        )

    reconstruction, expert_outputs = _reconstruction(
        tokens, weights, model.expert_weight, model.expert_bias
    )
    difference = reconstruction - tokens
    task_loss = float(np.mean(np.sum(difference * difference, axis=1)))
    d_reconstruction = (2.0 / float(token_count)) * difference

    expert_weight_gradient = np.zeros_like(model.expert_weight)
    expert_bias_gradient = np.zeros_like(model.expert_bias)
    d_weight = np.zeros_like(weights)
    for expert_index in range(num_experts):
        weighted_gradient = weights[:, expert_index, None] * d_reconstruction
        expert_weight_gradient[expert_index] = weighted_gradient.T @ tokens
        expert_bias_gradient[expert_index] = weighted_gradient.sum(axis=0)
        d_weight[:, expert_index] = np.sum(
            d_reconstruction * expert_outputs[expert_index], axis=1
        )

    balance_weight = float(model_config["balance_weight"])
    route_weight = float(model_config["route_weight"])
    capacity_weight = float(model_config["capacity_weight"])
    assignment_count = int(route.assignment_count)
    if assignment_count > 0:
        load_fraction = route.token_counts.astype(np.float64) / float(
            assignment_count
        )
        weight_sums = weights.sum(axis=0)
        total_weight = float(weight_sums.sum())
        if total_weight > 1.0e-15:
            gate_fraction = weight_sums / total_weight
            d_weight += (
                balance_weight
                * float(num_experts)
                * load_fraction[None, :]
                / total_weight
            ) * (weights > 0.0)
        else:
            gate_fraction = probabilities.mean(axis=0)
            gate_fraction = gate_fraction / float(gate_fraction.sum())
    else:
        load_fraction = np.zeros(num_experts, dtype=np.float64)
        gate_fraction = np.zeros(num_experts, dtype=np.float64)

    balance_loss = float(num_experts * np.dot(load_fraction, gate_fraction))
    scaled_logits = route.scaled_logits
    logsumexp = _logsumexp_rows(scaled_logits)
    route_loss = float(np.mean(logsumexp * logsumexp))
    capacity_loss = float(route.capacity_loss)
    total_loss = (
        task_loss
        + balance_weight * balance_loss
        + route_weight * route_loss
        + capacity_weight * capacity_loss
    )

    d_logits = np.zeros_like(probabilities)
    temperature = max(float(model.temperature), 1.0e-12)
    for token_index in range(token_count):
        selected = np.flatnonzero(assignments[token_index] > 0.0)
        if selected.size == 0:
            continue
        selected_probabilities = probabilities[token_index, selected]
        selected_total = float(selected_probabilities.sum())
        if selected_total <= 1.0e-15:
            continue
        selected_weight_gradient = d_weight[token_index, selected]
        d_probability_selected = (
            selected_total * selected_weight_gradient
            - selected_probabilities
            * float(np.dot(selected_probabilities, selected_weight_gradient))
        ) / (selected_total * selected_total)
        d_probability = np.zeros(probabilities.shape[1], dtype=np.float64)
        d_probability[selected] = d_probability_selected
        d_scaled = probabilities[token_index] * (
            d_probability
            - float(np.dot(probabilities[token_index], d_probability))
        )
        d_logits[token_index] += d_scaled / temperature

    d_scaled_route = (
        route_weight
        * (2.0 * logsumexp / float(token_count))[:, None]
        * probabilities
    )
    d_logits += d_scaled_route / temperature

    gate_weight_gradient = tokens.T @ d_logits
    gate_bias_gradient = d_logits.sum(axis=0)
    return (
        LossBreakdown(
            task_loss=task_loss,
            balance_loss=balance_loss,
            route_loss=route_loss,
            capacity_loss=capacity_loss,
            total_loss=float(total_loss),
        ),
        {
            "gate_weight": _clip_gradient(gate_weight_gradient),
            "gate_bias": _clip_gradient(gate_bias_gradient),
            "expert_weight": _clip_gradient(expert_weight_gradient),
            "expert_bias": _clip_gradient(expert_bias_gradient),
        },
    )


def evaluate_batch(
    model: ModelParameters,
    config: dict[str, Any],
    tokens: np.ndarray,
    masks: np.ndarray,
    sample_indices: np.ndarray,
) -> BatchEvaluation:
    active_tokens, active_sample_indices, valid = _flatten_batch(
        tokens, masks, sample_indices
    )
    route = route_tokens(
        model.gate,
        active_tokens,
        active_sample_indices,
        _router_config(config, model.temperature),
    )
    batch_shape = (tokens.shape[0], tokens.shape[1])
    full_assignments = _expand_active_matrix(
        route.assignments, valid, batch_shape
    )
    full_weights = _expand_active_matrix(
        route.activation_weights, valid, batch_shape
    )
    full_probabilities = _expand_active_matrix(route.probabilities, valid, batch_shape)
    full_pre_assignments = _expand_active_matrix(
        route.pre_assignment, valid, batch_shape
    )
    loss, _ = _loss_and_gradients(
        model,
        config,
        active_tokens,
        route.assignments,
        route.activation_weights,
        route.probabilities,
        route,
    )
    return BatchEvaluation(
        route=route,
        full_assignments=full_assignments,
        full_weights=full_weights,
        full_probabilities=full_probabilities,
        full_pre_assignments=full_pre_assignments,
        loss=loss,
    )


@dataclass
class TrainingResult:
    model: ModelParameters
    steps_completed: int
    adjustments: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    first_step_diagnostics: dict[str, Any]


class Trainer:
    def __init__(
        self,
        seed: int,
        config: dict[str, Any],
        tokens: np.ndarray,
        masks: np.ndarray,
        sample_ids: list[str],
        training_output_dir: str | Path,
        token_values: np.ndarray | None = None,
    ) -> None:
        self.seed = int(seed)
        self.config = config
        self.tokens = np.asarray(tokens, dtype=np.float64)
        self.masks = np.asarray(masks, dtype=np.int8)
        self.sample_ids = list(sample_ids)
        self.token_values = (
            np.asarray(token_values, dtype=np.float64)
            if token_values is not None
            else None
        )
        if self.token_values is not None and self.token_values.shape != self.masks.shape:
            raise ValueError(
                "token_values must have the same [sample, token] shape as masks"
            )
        self.output_dir = ensure_dir(training_output_dir)
        self.model = initialize_model(seed, config, self.tokens.shape[2])
        self.optimizer = AdamOptimizer(
            float(config["model"]["learning_rate"]),
            float(config["model"]["weight_decay"]),
        )
        self.rng = np.random.default_rng(seed + 101)
        self.low_utilization_streak = np.zeros(
            int(config["model"]["num_experts"]), dtype=np.int64
        )
        self.adjustment_count = 0
        self.last_adjustment_step = -10**9
        self.adjustments: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.step_record_path = self.output_dir / "step_records.jsonl"
        self.assignment_record_path = self.output_dir / "assignment_records.jsonl"
        self.alert_path = self.output_dir / "collapse_alerts.jsonl"
        for path in (
            self.step_record_path,
            self.assignment_record_path,
            self.alert_path,
        ):
            path.write_text("", encoding="utf-8")

        self.records: list[dict[str, Any]] = []
        self.step_tokens: list[np.ndarray] = []
        self.step_token_values: list[np.ndarray] = []
        self.step_masks: list[np.ndarray] = []
        self.step_sample_indices: list[np.ndarray] = []
        self.step_gate_weight: list[np.ndarray] = []
        self.step_gate_bias: list[np.ndarray] = []
        self.step_temperatures: list[float] = []
        self.step_probabilities: list[np.ndarray] = []
        self.step_pre_assignments: list[np.ndarray] = []
        self.step_assignments: list[np.ndarray] = []
        self.step_weights: list[np.ndarray] = []
        self.step_token_counts: list[np.ndarray] = []
        self.step_sample_counts: list[np.ndarray] = []
        self.step_utilization: list[np.ndarray] = []
        self.step_loss_balance: list[float] = []
        self.step_loss_route: list[float] = []
        self.step_loss_task: list[float] = []
        self.step_loss_capacity: list[float] = []
        self.step_loss_total: list[float] = []
        self.step_capacity: list[int] = []
        self.step_overflow: list[int] = []
        self.step_dropped: list[int] = []
        self.step_rerouted: list[int] = []
        self.step_near_ties: list[int] = []

    def _batch_schedule(self) -> list[np.ndarray]:
        sample_count = self.tokens.shape[0]
        batch_size = min(int(self.config["model"]["batch_size"]), sample_count)
        batches: list[np.ndarray] = []
        for _epoch in range(int(self.config["model"]["epochs"])):
            order = self.rng.permutation(sample_count)
            for start in range(0, sample_count - batch_size + 1, batch_size):
                batches.append(order[start : start + batch_size])
        if not batches:
            batches.append(np.arange(sample_count, dtype=np.int64))
        return batches

    def _parameter_snapshot(self) -> tuple[np.ndarray, np.ndarray, float]:
        return (
            self.model.gate.weight.copy(),
            self.model.gate.bias.copy(),
            float(self.model.temperature),
        )

    def _train_step(
        self, tokens: np.ndarray, masks: np.ndarray, sample_indices: np.ndarray
    ) -> tuple[BatchEvaluation, dict[str, np.ndarray]]:
        active_tokens, active_sample_indices, _valid = _flatten_batch(
            tokens, masks, sample_indices
        )
        route = route_tokens(
            self.model.gate,
            active_tokens,
            active_sample_indices,
            _router_config(self.config, self.model.temperature),
        )
        loss, gradients = _loss_and_gradients(
            self.model,
            self.config,
            active_tokens,
            route.assignments,
            route.activation_weights,
            route.probabilities,
            route,
        )
        batch_shape = (tokens.shape[0], tokens.shape[1])
        batch_eval = BatchEvaluation(
            route=route,
            full_assignments=_expand_active_matrix(
                route.assignments, _valid, batch_shape
            ),
            full_weights=_expand_active_matrix(
                route.activation_weights, _valid, batch_shape
            ),
            full_probabilities=_expand_active_matrix(
                route.probabilities, _valid, batch_shape
            ),
            full_pre_assignments=_expand_active_matrix(
                route.pre_assignment, _valid, batch_shape
            ),
            loss=loss,
        )
        self.optimizer.step(
            "gate_weight", self.model.gate.weight, gradients["gate_weight"]
        )
        self.optimizer.step("gate_bias", self.model.gate.bias, gradients["gate_bias"])
        self.optimizer.step(
            "expert_weight", self.model.expert_weight, gradients["expert_weight"]
        )
        self.optimizer.step(
            "expert_bias", self.model.expert_bias, gradients["expert_bias"]
        )
        return batch_eval, gradients

    def _append_arrays(
        self,
        tokens: np.ndarray,
        token_values: np.ndarray,
        masks: np.ndarray,
        sample_indices: np.ndarray,
        gate_weight: np.ndarray,
        gate_bias: np.ndarray,
        temperature: float,
        evaluation: BatchEvaluation,
    ) -> None:
        route = evaluation.route
        self.step_tokens.append(tokens.copy())
        self.step_token_values.append(token_values.copy())
        self.step_masks.append(masks.copy())
        self.step_sample_indices.append(sample_indices.copy())
        self.step_gate_weight.append(gate_weight.copy())
        self.step_gate_bias.append(gate_bias.copy())
        self.step_temperatures.append(float(temperature))
        self.step_probabilities.append(evaluation.full_probabilities.copy())
        self.step_pre_assignments.append(evaluation.full_pre_assignments.copy())
        self.step_assignments.append(evaluation.full_assignments.copy())
        self.step_weights.append(evaluation.full_weights.copy())
        self.step_token_counts.append(route.token_counts.copy())
        self.step_sample_counts.append(route.sample_counts.copy())
        self.step_utilization.append(route.utilization.copy())
        self.step_loss_balance.append(float(evaluation.loss.balance_loss))
        self.step_loss_route.append(float(evaluation.loss.route_loss))
        self.step_loss_task.append(float(evaluation.loss.task_loss))
        self.step_loss_capacity.append(float(evaluation.loss.capacity_loss))
        self.step_loss_total.append(float(evaluation.loss.total_loss))
        self.step_capacity.append(int(route.capacity_per_expert))
        self.step_overflow.append(int(route.overflow_events))
        self.step_dropped.append(int(route.dropped_slots))
        self.step_rerouted.append(int(route.rerouted_slots))
        self.step_near_ties.append(int(np.sum(route.near_tie_mask)))

    def _routing_diagnostics(
        self,
        evaluation: BatchEvaluation,
        masks: np.ndarray,
        sample_indices: np.ndarray,
    ) -> dict[str, Any]:
        route = evaluation.route
        token_length = masks.shape[1]
        active_full_indices = np.flatnonzero(masks.reshape(-1).astype(bool))
        near_tie_tokens: list[dict[str, Any]] = []
        for active_row in np.flatnonzero(route.near_tie_mask):
            probabilities = route.probabilities[active_row]
            order = np.argsort(-probabilities, kind="stable")
            top_k = int(self.config["model"]["top_k"])
            if top_k < probabilities.shape[0]:
                margin = float(
                    probabilities[order[top_k - 1]] - probabilities[order[top_k]]
                )
            else:
                margin = float(probabilities[order[top_k - 1]])
            full_row = int(active_full_indices[active_row])
            batch_row = full_row // token_length
            near_tie_tokens.append(
                {
                    "sample_id": self.sample_ids[int(sample_indices[batch_row])],
                    "token_index": int(full_row % token_length),
                    "topk_experts": route.topk_indices[active_row].tolist(),
                    "topk_probabilities": np.round(
                        probabilities[route.topk_indices[active_row]], 12
                    ).tolist(),
                    "affinity_margin": margin,
                }
            )

        capacity_affected_tokens: list[dict[str, Any]] = []
        pre_assignments = route.pre_assignment
        changed = np.any(
            pre_assignments.astype(np.int8)
            != route.assignments.astype(np.int8),
            axis=1,
        )
        for active_row in np.flatnonzero(changed):
            full_row = int(active_full_indices[active_row])
            batch_row = full_row // token_length
            final_experts = np.flatnonzero(
                route.assignments[active_row] > 0.0
            ).tolist()
            pre_experts = np.flatnonzero(
                pre_assignments[active_row] > 0.0
            ).tolist()
            capacity_affected_tokens.append(
                {
                    "sample_id": self.sample_ids[int(sample_indices[batch_row])],
                    "token_index": int(full_row % token_length),
                    "topk_experts": pre_experts,
                    "final_experts": final_experts,
                    "outcome": (
                        "rerouted" if final_experts else "dropped"
                    ),
                }
            )
        return {
            "near_tie_tokens": near_tie_tokens,
            "capacity_affected_tokens": capacity_affected_tokens,
        }

    def _track_collapse(
        self, step: int, utilization: np.ndarray, token_counts: np.ndarray
    ) -> list[dict[str, Any]]:
        threshold = float(self.config["collapse"]["utilization_threshold"])
        patience = int(self.config["collapse"]["patience_steps"])
        low = utilization < threshold
        self.low_utilization_streak = np.where(
            low, self.low_utilization_streak + 1, 0
        )
        alerts: list[dict[str, Any]] = []
        for expert_index in range(utilization.shape[0]):
            if self.low_utilization_streak[expert_index] >= patience:
                alert = {
                    "step": int(step),
                    "expert_id": int(expert_index),
                    "expert_utilization": float(utilization[expert_index]),
                    "utilization_threshold": threshold,
                    "consecutive_low_steps": int(
                        self.low_utilization_streak[expert_index]
                    ),
                    "token_count": int(token_counts[expert_index]),
                    "type": "persistent_low_expert_utilization",
                }
                alerts.append(alert)
                append_jsonl(self.alert_path, alert)
                self.alerts.append(alert)
        return alerts

    def _adjustment_summary(
        self, evaluation: BatchEvaluation
    ) -> dict[str, Any]:
        route = evaluation.route
        return {
            "temperature": float(self.model.temperature),
            "token_counts": route.token_counts.tolist(),
            "sample_counts": route.sample_counts.tolist(),
            "utilization": route.utilization.tolist(),
            "capacity_per_expert": int(route.capacity_per_expert),
            "overflow_events": int(route.overflow_events),
            "dropped_slots": int(route.dropped_slots),
            "rerouted_slots": int(route.rerouted_slots),
            "assignment_matrix": evaluation.full_assignments.astype(int).tolist(),
            "activation_weights": np.round(
                evaluation.full_weights,
                int(self.config["output"].get("float_decimals", 12)),
            ).tolist(),
            "losses": evaluation.loss.as_dict(),
        }

    def _maybe_adjust(
        self,
        step: int,
        alerts: list[dict[str, Any]],
        tokens: np.ndarray,
        masks: np.ndarray,
        sample_indices: np.ndarray,
    ) -> None:
        if not alerts:
            return
        collapse_config = self.config["collapse"]
        strategy = str(collapse_config["adjustment_strategy"])
        if strategy == "none":
            return
        if self.adjustment_count >= int(collapse_config["max_adjustments"]):
            return
        if (
            step - self.last_adjustment_step
            <= int(collapse_config["cooldown_steps"])
        ):
            return

        before_temperature = float(self.model.temperature)
        before = evaluate_batch(self.model, self.config, tokens, masks, sample_indices)
        reset_expert_parameters = False
        if strategy == "temperature":
            new_temperature = min(
                float(collapse_config["max_temperature"]),
                before_temperature
                * float(collapse_config["temperature_multiplier"]),
            )
            self.model.temperature = new_temperature
            applied = new_temperature > before_temperature + 1.0e-15
        elif strategy == "reinitialize_gate":
            adjustment_seed = (
                self.seed
                + int(collapse_config["reinit_seed_offset"])
                + 1009 * self.adjustment_count
            )
            self.model.gate.reset(
                adjustment_seed,
                self.config["model"]["gate_bias_init"],
            )
            self.optimizer.reset(("gate_weight", "gate_bias"))
            reset_expert_parameters = False
            applied = True
        else:
            return

        after = evaluate_batch(self.model, self.config, tokens, masks, sample_indices)
        after_temperature = float(self.model.temperature)
        event = {
            "step": int(step),
            "strategy": strategy,
            "applied": bool(applied),
            "triggering_alerts": alerts,
            "adjusted_experts": [alert["expert_id"] for alert in alerts],
            "temperature_before": before_temperature,
            "temperature_after": after_temperature,
            "gate_reinitialized": bool(strategy == "reinitialize_gate"),
            "expert_parameters_reinitialized": reset_expert_parameters,
            "before": self._adjustment_summary(before),
            "after": self._adjustment_summary(after),
            "loss_delta": {
                key: float(
                    after.loss.as_dict()[key] - before.loss.as_dict()[key]
                )
                for key in before.loss.as_dict()
            },
        }
        self.adjustments.append(event)
        self.adjustment_count += 1
        self.last_adjustment_step = int(step)
        self.low_utilization_streak[:] = 0
        ensure_dir(self.output_dir / "adjustments")
        np.savez_compressed(
            self.output_dir
            / "adjustments"
            / f"step_{step:04d}_{strategy}.npz",
            before_assignments=before.full_assignments.astype(np.int8),
            before_weights=before.full_weights,
            before_probabilities=before.full_probabilities,
            after_assignments=after.full_assignments.astype(np.int8),
            after_weights=after.full_weights,
            after_probabilities=after.full_probabilities,
            token_counts_before=before.route.token_counts,
            token_counts_after=after.route.token_counts,
            utilization_before=before.route.utilization,
            utilization_after=after.route.utilization,
        )

    def _write_step_arrays(self) -> None:
        steps = np.arange(1, len(self.records) + 1, dtype=np.int64)

        def stack(values: list[Any], dtype: Any | None = None) -> np.ndarray:
            if dtype is None:
                return np.stack(values)
            return np.stack(values).astype(dtype)

        arrays_to_save: dict[str, Any] = {
            "steps": steps,
            "tokens": stack(self.step_tokens),
            "masks": stack(self.step_masks, np.int8),
            "sample_indices": stack(self.step_sample_indices, np.int64),
            "gate_weight": stack(self.step_gate_weight),
            "gate_bias": stack(self.step_gate_bias),
            "temperatures": np.asarray(self.step_temperatures, dtype=np.float64),
            "probabilities": stack(self.step_probabilities),
            "pre_assignments": stack(self.step_pre_assignments, np.int8),
            "assignments": stack(self.step_assignments, np.int8),
            "activation_weights": stack(self.step_weights),
            "token_counts": stack(self.step_token_counts, np.int64),
            "sample_counts": stack(self.step_sample_counts, np.int64),
            "utilization": stack(self.step_utilization),
            "balance_loss": np.asarray(self.step_loss_balance, dtype=np.float64),
            "route_loss": np.asarray(self.step_loss_route, dtype=np.float64),
            "task_loss": np.asarray(self.step_loss_task, dtype=np.float64),
            "capacity_loss": np.asarray(self.step_loss_capacity, dtype=np.float64),
            "total_loss": np.asarray(self.step_loss_total, dtype=np.float64),
            "capacity_per_expert": np.asarray(self.step_capacity, dtype=np.int64),
            "overflow_events": np.asarray(self.step_overflow, dtype=np.int64),
            "dropped_slots": np.asarray(self.step_dropped, dtype=np.int64),
            "rerouted_slots": np.asarray(self.step_rerouted, dtype=np.int64),
            "near_tie_token_counts": np.asarray(
                self.step_near_ties, dtype=np.int64
            ),
        }
        if self.step_token_values:
            arrays_to_save["token_values"] = stack(self.step_token_values)
        np.savez_compressed(
            self.output_dir / "step_arrays.npz",
            **arrays_to_save,
        )

    def run(self) -> TrainingResult:
        batches = self._batch_schedule()
        first_diagnostics: dict[str, Any] = {}
        for step_number, sample_indices in enumerate(batches, start=1):
            tokens = self.tokens[sample_indices]
            masks = self.masks[sample_indices]
            scalar_values = (
                self.token_values[sample_indices]
                if self.token_values is not None
                else np.zeros_like(masks, dtype=np.float64)
            )
            gate_weight, gate_bias, temperature = self._parameter_snapshot()
            evaluation, _gradients = self._train_step(
                tokens, masks, sample_indices
            )
            self._append_arrays(
                tokens,
                scalar_values,
                masks,
                sample_indices,
                gate_weight,
                gate_bias,
                temperature,
                evaluation,
            )
            route = evaluation.route
            diagnostics = self._routing_diagnostics(
                evaluation, masks, sample_indices
            )
            record = {
                "step": int(step_number),
                "sample_indices": sample_indices.tolist(),
                "sample_ids": [self.sample_ids[index] for index in sample_indices],
                "router_parameters": "pre_optimizer_update_snapshot",
                "temperature": float(temperature),
                "active_token_count": int(route.active_token_count),
                "assignment_count": int(route.assignment_count),
                "capacity_per_expert": int(route.capacity_per_expert),
                "token_counts": route.token_counts.tolist(),
                "sample_counts": route.sample_counts.tolist(),
                "utilization": route.utilization.tolist(),
                "overflow_events": int(route.overflow_events),
                "dropped_slots": int(route.dropped_slots),
                "rerouted_slots": int(route.rerouted_slots),
                "tokens_without_assignment": int(route.tokens_without_assignment),
                "tokens_below_top_k": int(route.tokens_below_top_k),
                "near_tie_token_count": int(np.sum(route.near_tie_mask)),
                "near_tie_tokens": diagnostics["near_tie_tokens"],
                "capacity_affected_token_count": len(
                    diagnostics["capacity_affected_tokens"]
                ),
                "assignment_sha256": array_sha256(
                    evaluation.full_assignments.astype(np.int8)
                ),
                "activation_weights_sha256": array_sha256(
                    evaluation.full_weights
                ),
                "losses": evaluation.loss.as_dict(),
            }
            self.records.append(record)
            append_jsonl(self.step_record_path, record)
            append_jsonl(
                self.assignment_record_path,
                {
                    "step": int(step_number),
                    "sample_indices": sample_indices.tolist(),
                    "sample_ids": [self.sample_ids[index] for index in sample_indices],
                    "mask": masks.astype(int).tolist(),
                    "normalized_token_values": np.round(
                        scalar_values,
                        int(self.config["output"].get("float_decimals", 12)),
                    ).tolist(),
                    "assignments": evaluation.full_assignments.astype(int).tolist(),
                    "activation_weights": np.round(
                        evaluation.full_weights,
                        int(self.config["output"].get("float_decimals", 12)),
                    ).tolist(),
                    "near_tie_tokens": diagnostics["near_tie_tokens"],
                    "capacity_affected_tokens": diagnostics[
                        "capacity_affected_tokens"
                    ],
                },
            )
            if step_number == 1:
                first_diagnostics = {
                    "step": 1,
                    "capacity_per_expert": int(route.capacity_per_expert),
                    "near_tie_token_count": int(np.sum(route.near_tie_mask)),
                    "overflow_events": int(route.overflow_events),
                    "assignment_count": int(route.assignment_count),
                }

            alerts = self._track_collapse(
                step_number, route.utilization, route.token_counts
            )
            self._maybe_adjust(
                step_number, alerts, tokens, masks, sample_indices
            )

        self._write_step_arrays()
        write_json(self.output_dir / "adjustments.json", self.adjustments)
        write_json(
            self.output_dir / "training_summary.json",
            {
                "seed": self.seed,
                "steps_completed": len(self.records),
                "num_experts": int(self.config["model"]["num_experts"]),
                "top_k": int(self.config["model"]["top_k"]),
                "capacity_factor": float(
                    self.config["model"]["capacity_factor"]
                ),
                "overflow_strategy": self.config["model"]["overflow_strategy"],
                "initial_temperature": float(
                    self.config["model"]["initial_temperature"]
                ),
                "final_temperature": float(self.model.temperature),
                "adjustment_count": int(self.adjustment_count),
                "alert_count": len(self.alerts),
                "first_step_diagnostics": first_diagnostics,
                "final_losses": self.records[-1]["losses"] if self.records else {},
            },
        )
        return TrainingResult(
            model=self.model,
            steps_completed=len(self.records),
            adjustments=self.adjustments,
            alerts=self.alerts,
            first_step_diagnostics=first_diagnostics,
        )


def save_model_snapshot(
    model: ModelParameters,
    *,
    output_dir: str | Path,
    seed: int,
    config_fingerprint: str,
    config: dict[str, Any],
) -> Path:
    output_dir = ensure_dir(output_dir)
    path = output_dir / "model_snapshot.npz"
    np.savez_compressed(
        path,
        gate_weight=model.gate.weight,
        gate_bias=model.gate.bias,
        expert_weight=model.expert_weight,
        expert_bias=model.expert_bias,
        temperature=np.asarray(model.temperature, dtype=np.float64),
        seed=np.asarray(seed, dtype=np.int64),
        config_fingerprint=np.asarray(config_fingerprint),
        num_experts=np.asarray(config["model"]["num_experts"], dtype=np.int64),
        top_k=np.asarray(config["model"]["top_k"], dtype=np.int64),
        token_dim=np.asarray(model.gate.weight.shape[0], dtype=np.int64),
    )
    return path
