"""Independent reference router used to audit training records.

This module intentionally does not import :mod:`spectral_workbench.routing`.
It reimplements the documented fixed routing rules directly from normalized
spectra, gate snapshots, and capacity settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class ReferenceRoute:
    logits: np.ndarray
    probabilities: np.ndarray
    topk_indices: np.ndarray
    assignments: np.ndarray
    activation_weights: np.ndarray
    token_counts: np.ndarray
    sample_counts: np.ndarray
    utilization: np.ndarray
    balance_loss: float
    capacity_per_expert: int


def _ref_softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=1, keepdims=True)


def reference_route(
    *,
    tokens: np.ndarray,
    sample_indices: np.ndarray,
    gate_weight: np.ndarray,
    gate_bias: np.ndarray,
    temperature: float,
    num_experts: int,
    top_k: int,
    capacity_factor: float,
    overflow_strategy: str,
) -> ReferenceRoute:
    tokens = np.asarray(tokens, dtype=np.float64)
    sample_indices = np.asarray(sample_indices)
    token_count = int(tokens.shape[0])
    logits = tokens @ gate_weight + gate_bias
    probabilities = _ref_softmax(logits / float(temperature))
    topk_indices = np.argsort(-probabilities, axis=1, kind="stable")[:, :top_k]

    capacity = max(
        1, int(np.floor(capacity_factor * token_count * top_k / float(num_experts)))
    )
    counts = np.zeros(num_experts, dtype=np.int64)
    assignments = np.zeros((token_count, num_experts), dtype=np.float64)
    candidate_rows: list[tuple[float, int, int, int]] = []
    for token_index in range(token_count):
        for rank, expert_index in enumerate(topk_indices[token_index]):
            candidate_rows.append(
                (
                    -float(probabilities[token_index, expert_index]),
                    token_index,
                    int(rank),
                    int(expert_index),
                )
            )
    candidate_rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))

    for _score, token_index, _rank, expert_index in candidate_rows:
        if int(assignments[token_index].sum()) >= top_k:
            continue
        if assignments[token_index, expert_index] != 0.0:
            continue
        if counts[expert_index] < capacity:
            assignments[token_index, expert_index] = 1.0
            counts[expert_index] += 1
            continue
        if overflow_strategy == "drop":
            continue
        for alternative in np.argsort(-probabilities[token_index], kind="stable"):
            alternative = int(alternative)
            if assignments[token_index, alternative] != 0.0:
                continue
            if counts[alternative] >= capacity:
                continue
            assignments[token_index, alternative] = 1.0
            counts[alternative] += 1
            break

    activation_weights = np.zeros_like(probabilities)
    for token_index in range(token_count):
        selected = assignments[token_index] > 0.0
        if not selected.any():
            continue
        selected_probabilities = probabilities[token_index, selected]
        denominator = float(selected_probabilities.sum())
        if denominator > 1.0e-15:
            activation_weights[token_index, selected] = (
                selected_probabilities / denominator
            )
        else:
            activation_weights[token_index, selected] = (
                1.0 / float(selected.sum())
            )

    assignment_total = int(counts.sum())
    token_counts = counts.copy()
    utilization = (
        token_counts.astype(np.float64) / float(assignment_total)
        if assignment_total > 0
        else np.zeros(num_experts, dtype=np.float64)
    )
    sample_counts = np.zeros(num_experts, dtype=np.int64)
    for expert_index in range(num_experts):
        selected = assignments[:, expert_index] > 0.0
        if selected.any():
            sample_counts[expert_index] = int(
                np.unique(sample_indices[selected]).size
            )

    if assignment_total > 0:
        load_fraction = token_counts.astype(np.float64) / float(assignment_total)
        weight_sums = activation_weights.sum(axis=0)
        if float(weight_sums.sum()) > 1.0e-15:
            gate_fraction = weight_sums / float(weight_sums.sum())
        else:
            gate_fraction = probabilities.mean(axis=0)
            gate_fraction = gate_fraction / float(gate_fraction.sum())
        balance_loss = float(num_experts * np.dot(load_fraction, gate_fraction))
    else:
        balance_loss = 0.0

    return ReferenceRoute(
        logits=logits,
        probabilities=probabilities,
        topk_indices=topk_indices,
        assignments=assignments,
        activation_weights=activation_weights,
        token_counts=token_counts,
        sample_counts=sample_counts,
        utilization=utilization,
        balance_loss=balance_loss,
        capacity_per_expert=capacity,
    )


def _first_assignment_mismatch_basis(
    *,
    training_assignments: np.ndarray,
    reference_route_result: ReferenceRoute,
    training_weights: np.ndarray,
    tokens: np.ndarray,
    scalar_token_values: np.ndarray | None,
    sample_indices: np.ndarray,
    active_full_indices: np.ndarray,
    token_length: int,
) -> list[dict[str, Any]]:
    weight_mismatch = np.any(
        np.abs(training_weights - reference_route_result.activation_weights)
        > 1.0e-12,
        axis=1,
    )
    mismatched_rows = np.flatnonzero(
        np.any(
            training_assignments.astype(np.int8)
            != reference_route_result.assignments.astype(np.int8),
            axis=1,
        )
        | weight_mismatch
    )
    basis: list[dict[str, Any]] = []
    for active_row in mismatched_rows[:16]:
        full_row = int(active_full_indices[active_row])
        batch_row = full_row // token_length
        token_offset = full_row % token_length
        basis.append(
            {
                "active_token_row": int(active_row),
                "full_flattened_token_row": full_row,
                "sample_index": int(sample_indices[active_row]),
                "token_index_in_sample": token_offset,
                "batch_row": batch_row,
                "normalized_scalar_token": (
                    float(scalar_token_values[active_row])
                    if scalar_token_values is not None
                    else None
                ),
                "normalized_token": np.round(tokens[active_row], 12).tolist(),
                "logits": np.round(
                    reference_route_result.logits[active_row], 12
                ).tolist(),
                "probabilities": np.round(
                    reference_route_result.probabilities[active_row], 12
                ).tolist(),
                "topk_experts": reference_route_result.topk_indices[
                    active_row
                ].tolist(),
                "training_assignment": training_assignments[
                    active_row
                ].astype(int).tolist(),
                "reference_assignment": reference_route_result.assignments[
                    active_row
                ].astype(int).tolist(),
                "training_activation_weights": np.round(
                    training_weights[active_row], 12
                ).tolist(),
                "reference_activation_weights": np.round(
                    reference_route_result.activation_weights[active_row], 12
                ).tolist(),
            }
        )
    return basis


def verify_training_records(
    step_arrays_path: str | Path,
    config: dict[str, Any],
    *,
    assignment_tolerance: float = 0.0,
    weight_tolerance: float = 1.0e-12,
    loss_tolerance: float | None = None,
) -> dict[str, Any]:
    if loss_tolerance is None:
        loss_tolerance = float(config["evaluation"]["loss_tolerance"])
    model_config = config["model"]
    token_length = int(config["generator"]["token_length"])
    per_step: list[dict[str, Any]] = []
    first_mismatch: dict[str, Any] | None = None

    with np.load(Path(step_arrays_path), allow_pickle=False) as arrays:
        steps = arrays["steps"]
        for index, step in enumerate(steps):
            tokens_full = arrays["tokens"][index]
            masks_full = arrays["masks"][index].astype(bool)
            local_sample_indices = arrays["sample_indices"][index]
            flat_tokens = tokens_full.reshape(-1, tokens_full.shape[-1])
            flat_masks = masks_full.reshape(-1)
            active_full_indices = np.flatnonzero(flat_masks)
            active_tokens = flat_tokens[flat_masks]
            scalar_token_values = None
            if "token_values" in arrays.files:
                scalar_token_values = arrays["token_values"][index].reshape(-1)[
                    flat_masks
                ]
            active_sample_indices = np.repeat(
                local_sample_indices, token_length
            )[flat_masks]
            training_assignments_full = arrays["assignments"][index]
            training_weights_full = arrays["activation_weights"][index]
            training_assignments = training_assignments_full.reshape(
                -1, training_assignments_full.shape[-1]
            )[flat_masks]
            training_weights = training_weights_full.reshape(
                -1, training_weights_full.shape[-1]
            )[flat_masks]
            reference = reference_route(
                tokens=active_tokens,
                sample_indices=active_sample_indices,
                gate_weight=arrays["gate_weight"][index],
                gate_bias=arrays["gate_bias"][index],
                temperature=float(arrays["temperatures"][index]),
                num_experts=int(model_config["num_experts"]),
                top_k=int(model_config["top_k"]),
                capacity_factor=float(model_config["capacity_factor"]),
                overflow_strategy=str(model_config["overflow_strategy"]),
            )

            assignment_matches = bool(
                np.allclose(
                    training_assignments,
                    reference.assignments,
                    atol=assignment_tolerance,
                    rtol=0.0,
                )
            )
            weights_match = bool(
                np.allclose(
                    training_weights,
                    reference.activation_weights,
                    atol=weight_tolerance,
                    rtol=0.0,
                )
            )
            token_counts_match = bool(
                np.array_equal(
                    arrays["token_counts"][index].astype(np.int64),
                    reference.token_counts,
                )
            )
            sample_counts_match = bool(
                np.array_equal(
                    arrays["sample_counts"][index].astype(np.int64),
                    reference.sample_counts,
                )
            )
            training_balance = float(arrays["balance_loss"][index])
            balance_match = bool(
                abs(training_balance - reference.balance_loss) <= loss_tolerance
            )
            capacity_match = bool(
                int(arrays["capacity_per_expert"][index])
                == reference.capacity_per_expert
            )
            verified = bool(
                assignment_matches
                and weights_match
                and token_counts_match
                and sample_counts_match
                and balance_match
                and capacity_match
            )
            step_entry = {
                "step": int(step),
                "verified": verified,
                "assignment_matches": assignment_matches,
                "activation_weights_match": weights_match,
                "token_counts_match": token_counts_match,
                "sample_counts_match": sample_counts_match,
                "balance_loss_matches": balance_match,
                "capacity_per_expert_matches": capacity_match,
                "training_token_counts": arrays["token_counts"][index].tolist(),
                "reference_token_counts": reference.token_counts.tolist(),
                "training_balance_loss": training_balance,
                "reference_balance_loss": float(reference.balance_loss),
                "capacity_per_expert": int(reference.capacity_per_expert),
                "training_capacity_per_expert": int(
                    arrays["capacity_per_expert"][index]
                ),
            }
            per_step.append(step_entry)

            if not verified and first_mismatch is None:
                mismatch_types: list[str] = []
                if not assignment_matches:
                    mismatch_types.append("assignment_matrix")
                if not weights_match:
                    mismatch_types.append("activation_weights")
                if not token_counts_match:
                    mismatch_types.append("token_counts")
                if not sample_counts_match:
                    mismatch_types.append("sample_counts")
                if not balance_match:
                    mismatch_types.append("balance_loss")
                if not capacity_match:
                    mismatch_types.append("capacity_per_expert")
                first_mismatch = {
                    **step_entry,
                    "mismatch_types": mismatch_types,
                    "routing_basis": {
                        "gate_weight": arrays["gate_weight"][index].tolist(),
                        "gate_bias": arrays["gate_bias"][index].tolist(),
                        "temperature": float(arrays["temperatures"][index]),
                        "top_k": int(model_config["top_k"]),
                        "capacity_factor": float(
                            model_config["capacity_factor"]
                        ),
                        "overflow_strategy": str(
                            model_config["overflow_strategy"]
                        ),
                        "token_level": _first_assignment_mismatch_basis(
                            training_assignments=training_assignments,
                            reference_route_result=reference,
                            training_weights=training_weights,
                            tokens=active_tokens,
                            scalar_token_values=scalar_token_values,
                            sample_indices=active_sample_indices,
                            active_full_indices=active_full_indices,
                            token_length=token_length,
                        )
                        if not assignment_matches or not weights_match
                        else [],
                    },
                }

    return {
        "reference": "independent_fixed_topk_capacity_router_v1",
        "verified": first_mismatch is None,
        "steps_checked": len(per_step),
        "first_mismatch_step": (
            int(first_mismatch["step"]) if first_mismatch is not None else None
        ),
        "first_mismatch": first_mismatch,
        "per_step": per_step,
    }
