"""Cross-version audits and expert specialization reports."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from .formats import NormalizedDataset
from .embedding import build_token_features
from .routing import RouterConfig, route_tokens
from .training import ModelParameters
from .utils import ensure_dir, write_json


def _router_config(
    model: ModelParameters, config: dict[str, Any]
) -> RouterConfig:
    model_config = config["model"]
    return RouterConfig(
        num_experts=int(model_config["num_experts"]),
        top_k=int(model_config["top_k"]),
        temperature=float(model.temperature),
        capacity_factor=float(model_config["capacity_factor"]),
        overflow_strategy=str(model_config["overflow_strategy"]),
        near_tie_margin=float(model_config["near_tie_margin"]),
    )


def _route_dataset(
    model: ModelParameters,
    dataset: NormalizedDataset,
    config: dict[str, Any],
    *,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    samples = dataset.samples
    if sample_limit is not None:
        samples = samples[:sample_limit]
    scalar_values = np.stack([sample.token_values for sample in samples])
    masks = np.stack([sample.mask for sample in samples])
    values = build_token_features(scalar_values, masks, config)
    batch_size, token_length, token_dim = values.shape
    flat_values = values.reshape(batch_size * token_length, token_dim)
    flat_masks = masks.reshape(batch_size * token_length).astype(bool)
    active_tokens = flat_values[flat_masks]
    sample_indices = np.repeat(np.arange(batch_size, dtype=np.int64), token_length)[
        flat_masks
    ]
    route = route_tokens(
        model.gate,
        active_tokens,
        sample_indices,
        _router_config(model, config),
    )
    full_assignments = np.zeros(
        (batch_size * token_length, route.assignments.shape[1]), dtype=np.float64
    )
    full_weights = np.zeros_like(full_assignments)
    full_probabilities = np.zeros_like(full_assignments)
    full_assignments[flat_masks] = route.assignments
    full_weights[flat_masks] = route.activation_weights
    full_probabilities[flat_masks] = route.probabilities
    return {
        "dataset": dataset,
        "samples": samples,
        "sample_ids": [sample.sample_id for sample in samples],
        "material_families": [sample.material_family for sample in samples],
        "instrument_sources": [sample.instrument_source for sample in samples],
        "modalities": [sample.modality for sample in samples],
        "is_rare": np.asarray([sample.is_rare for sample in samples], dtype=bool),
        "scalar_values": scalar_values,
        "values": values,
        "masks": masks,
        "flat_masks": flat_masks,
        "route": route,
        "assignments": full_assignments.reshape(batch_size, token_length, -1),
        "weights": full_weights.reshape(batch_size, token_length, -1),
        "probabilities": full_probabilities.reshape(batch_size, token_length, -1),
        "token_length": token_length,
    }


def _flatten_token_mismatches(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    version: str,
    value_tolerance: float,
    weight_tolerance: float,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    reference_masks = reference["masks"]
    candidate_masks = candidate["masks"]
    value_differences = np.abs(
        reference["scalar_values"] - candidate["scalar_values"]
    )
    assignment_differences = np.any(
        reference["assignments"].astype(np.int8)
        != candidate["assignments"].astype(np.int8),
        axis=-1,
    )
    probability_differences = np.any(
        np.abs(reference["probabilities"] - candidate["probabilities"])
        > weight_tolerance,
        axis=-1,
    )
    weight_differences = np.any(
        np.abs(reference["weights"] - candidate["weights"]) > weight_tolerance,
        axis=-1,
    )
    mask_differences = reference_masks != candidate_masks
    value_mismatch_mask = value_differences > value_tolerance
    mismatch_mask = (
        mask_differences
        | value_mismatch_mask
        | assignment_differences
        | probability_differences
        | weight_differences
    )
    for sample_index, token_index in np.argwhere(mismatch_mask):
        categories: list[str] = []
        if mask_differences[sample_index, token_index]:
            categories.append("mask_mismatch")
        if value_mismatch_mask[sample_index, token_index]:
            categories.append("normalization_value_mismatch")
        if assignment_differences[sample_index, token_index]:
            categories.append("assignment_mismatch")
        if weight_differences[sample_index, token_index]:
            categories.append("activation_weight_mismatch")
        if probability_differences[sample_index, token_index]:
            categories.append("gate_probability_mismatch")
        differences.append(
            {
                "sample_id": reference["sample_ids"][sample_index],
                "token_index": int(token_index),
                "candidate_version": version,
                "reason_categories": categories,
                "reference_mask": int(reference_masks[sample_index, token_index]),
                "candidate_mask": int(candidate_masks[sample_index, token_index]),
                "token_value_abs_diff": float(
                    value_differences[sample_index, token_index]
                ),
                "reference_assignment": reference["assignments"][
                    sample_index, token_index
                ].astype(int).tolist(),
                "candidate_assignment": candidate["assignments"][
                    sample_index, token_index
                ].astype(int).tolist(),
            }
        )
    return differences


def build_cross_version_report(
    model: ModelParameters,
    datasets: dict[str, NormalizedDataset],
    config: dict[str, Any],
    *,
    output_dir: str | Path,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    if sample_limit is None:
        sample_limit = int(config["evaluation"]["batch_size"])
    evaluation = {
        version: _route_dataset(
            model, dataset, config, sample_limit=sample_limit
        )
        for version, dataset in datasets.items()
    }
    reference = evaluation["A"]
    value_tolerance = float(config["evaluation"]["value_tolerance"])
    loss_tolerance = float(config["evaluation"]["loss_tolerance"])
    weight_tolerance = max(1.0e-12, value_tolerance)
    version_reports: dict[str, Any] = {}
    differences: list[dict[str, Any]] = []

    for version in ("A", "B", "C"):
        candidate = evaluation[version]
        version_differences = _flatten_token_mismatches(
            reference,
            candidate,
            version,
            value_tolerance,
            weight_tolerance,
        )
        counts_match = np.array_equal(
            reference["route"].token_counts,
            candidate["route"].token_counts,
        )
        sample_counts_match = np.array_equal(
            reference["route"].sample_counts,
            candidate["route"].sample_counts,
        )
        balance_match = (
            abs(
                reference["route"].balance_loss
                - candidate["route"].balance_loss
            )
            <= loss_tolerance
        )
        version_reports[version] = {
            "token_mismatch_count": len(version_differences),
            "token_counts": candidate["route"].token_counts.tolist(),
            "sample_counts": candidate["route"].sample_counts.tolist(),
            "utilization": candidate["route"].utilization.tolist(),
            "balance_loss": float(candidate["route"].balance_loss),
            "route_loss": float(candidate["route"].route_loss),
            "capacity_loss": float(candidate["route"].capacity_loss),
            "token_counts_match_reference_a": bool(counts_match),
            "sample_counts_match_reference_a": bool(sample_counts_match),
            "balance_loss_match_reference_a": bool(balance_match),
        }
        if not counts_match:
            for expert_index, (expected, actual) in enumerate(
                zip(
                    reference["route"].token_counts,
                    candidate["route"].token_counts,
                )
            ):
                if expected != actual:
                    differences.append(
                        {
                            "sample_id": None,
                            "token_index": None,
                            "candidate_version": version,
                            "reason_categories": ["load_count_mismatch"],
                            "expert_id": int(expert_index),
                            "reference_count": int(expected),
                            "candidate_count": int(actual),
                        }
                    )
        if not balance_match:
            differences.append(
                {
                    "sample_id": None,
                    "token_index": None,
                    "candidate_version": version,
                    "reason_categories": ["balance_loss_mismatch"],
                    "reference_balance_loss": float(
                        reference["route"].balance_loss
                    ),
                    "candidate_balance_loss": float(
                        candidate["route"].balance_loss
                    ),
                }
            )
        differences.extend(version_differences)

    consistent = all(
        report["token_mismatch_count"] == 0
        and report["token_counts_match_reference_a"]
        and report["sample_counts_match_reference_a"]
        and report["balance_loss_match_reference_a"]
        for report in version_reports.values()
    )
    sample_ids = reference["sample_ids"]
    output_dir = ensure_dir(output_dir)
    np.savez_compressed(
        output_dir / "cross_version_arrays.npz",
        sample_ids=np.asarray(sample_ids),
        version_A_assignments=evaluation["A"]["assignments"].astype(np.int8),
        version_B_assignments=evaluation["B"]["assignments"].astype(np.int8),
        version_C_assignments=evaluation["C"]["assignments"].astype(np.int8),
        version_A_weights=evaluation["A"]["weights"],
        version_B_weights=evaluation["B"]["weights"],
        version_C_weights=evaluation["C"]["weights"],
        version_A_token_counts=evaluation["A"]["route"].token_counts,
        version_B_token_counts=evaluation["B"]["route"].token_counts,
        version_C_token_counts=evaluation["C"]["route"].token_counts,
        version_A_balance_loss=np.asarray(
            evaluation["A"]["route"].balance_loss
        ),
        version_B_balance_loss=np.asarray(
            evaluation["B"]["route"].balance_loss
        ),
        version_C_balance_loss=np.asarray(
            evaluation["C"]["route"].balance_loss
        ),
    )
    report = {
        "sample_ids": sample_ids,
        "sample_count": len(sample_ids),
        "versions_compared": ["A", "B", "C"],
        "value_tolerance": value_tolerance,
        "loss_tolerance": loss_tolerance,
        "consistent": bool(consistent),
        "versions": version_reports,
        "difference_count": len(differences),
        "differences": differences,
        "assignment_array_path": "cross_version_arrays.npz",
    }
    write_json(output_dir / "cross_version_check.json", report)
    return report


def _group_report(
    group_key: str,
    group_label: str,
    indices: np.ndarray,
    evaluation: dict[str, Any],
    threshold: float,
    is_rare_group: bool,
) -> dict[str, Any]:
    assignments = evaluation["assignments"]
    weights = evaluation["weights"]
    masks = evaluation["masks"]
    num_experts = assignments.shape[-1]
    selected_assignments = assignments[indices][masks[indices].astype(bool)]
    selected_weights = weights[indices][masks[indices].astype(bool)]
    token_count = selected_assignments.shape[0]
    assignment_counts = (
        selected_assignments.sum(axis=0).astype(np.int64)
        if token_count
        else np.zeros(num_experts, dtype=np.int64)
    )
    total_assignments = int(assignment_counts.sum())
    assignment_share = (
        assignment_counts.astype(np.float64) / float(total_assignments)
        if total_assignments > 0
        else np.zeros(num_experts, dtype=np.float64)
    )
    if token_count:
        primary_experts = np.argmax(selected_weights, axis=1)
        primary_counts = np.bincount(
            primary_experts, minlength=num_experts
        ).astype(np.int64)
        primary_token_share = primary_counts.astype(np.float64) / float(
            token_count
        )
        activation_weight_mean = selected_weights.sum(axis=0) / float(token_count)
    else:
        primary_counts = np.zeros(num_experts, dtype=np.int64)
        primary_token_share = np.zeros(num_experts, dtype=np.float64)
        activation_weight_mean = np.zeros(num_experts, dtype=np.float64)
    maximum_share = float(np.max(primary_token_share)) if num_experts else 0.0
    concentrated = maximum_share > threshold
    rare_warning = bool(is_rare_group and concentrated)
    return {
        "grouping": group_key,
        "group": group_label,
        "sample_indices": indices.tolist(),
        "sample_count": int(indices.size),
        "valid_token_count": int(token_count),
        "assignment_counts": assignment_counts.tolist(),
        "assignment_token_share": assignment_share.tolist(),
        "primary_expert_counts": primary_counts.tolist(),
        "expert_token_share": primary_token_share.tolist(),
        "activation_weight_mean": activation_weight_mean.tolist(),
        "max_primary_expert_share": maximum_share,
        "concentration_threshold": threshold,
        "concentrated_above_threshold": bool(concentrated),
        "rare_material_group": bool(is_rare_group),
        "rare_concentration_warning": rare_warning,
    }


def build_specialization_report(
    model: ModelParameters,
    dataset: NormalizedDataset,
    config: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    evaluation = _route_dataset(model, dataset, config)
    threshold = float(config["evaluation"]["concentration_threshold"])
    material_families = evaluation["material_families"]
    instruments = evaluation["instrument_sources"]
    rare_families = set(config["generator"]["rare_material_families"])
    groups: list[dict[str, Any]] = []

    material_groups: dict[str, list[int]] = defaultdict(list)
    instrument_groups: dict[str, list[int]] = defaultdict(list)
    combined_groups: dict[str, list[int]] = defaultdict(list)
    for index, (material, instrument) in enumerate(
        zip(material_families, instruments)
    ):
        material_groups[material].append(index)
        instrument_groups[instrument].append(index)
        combined_groups[f"{material}|{instrument}"].append(index)

    for material in sorted(material_groups):
        groups.append(
            _group_report(
                "material_family",
                material,
                np.asarray(material_groups[material], dtype=np.int64),
                evaluation,
                threshold,
                material in rare_families,
            )
        )
    for instrument in sorted(instrument_groups):
        groups.append(
            _group_report(
                "instrument_source",
                instrument,
                np.asarray(instrument_groups[instrument], dtype=np.int64),
                evaluation,
                threshold,
                False,
            )
        )
    for combined in sorted(combined_groups):
        material = combined.split("|", 1)[0]
        groups.append(
            _group_report(
                "material_family__instrument_source",
                combined,
                np.asarray(combined_groups[combined], dtype=np.int64),
                evaluation,
                threshold,
                material in rare_families,
            )
        )

    warnings = [group for group in groups if group["rare_concentration_warning"]]
    report = {
        "concentration_threshold": threshold,
        "primary_expert_definition": (
            "expert with largest capacity-constrained activation weight for each "
            "valid token; ties are resolved by lowest expert id"
        ),
        "activation_weight_mean_definition": (
            "sum of an expert's activation weights divided by all valid tokens in "
            "the group, including zero weights"
        ),
        "sample_count": len(evaluation["sample_ids"]),
        "valid_token_count": int(evaluation["flat_masks"].sum()),
        "token_counts": evaluation["route"].token_counts.tolist(),
        "sample_counts": evaluation["route"].sample_counts.tolist(),
        "utilization": evaluation["route"].utilization.tolist(),
        "balance_loss": float(evaluation["route"].balance_loss),
        "rare_concentration_warning_count": len(warnings),
        "rare_concentration_warnings": warnings,
        "groups": groups,
    }
    output_dir = ensure_dir(output_dir)
    np.savez_compressed(
        output_dir / "specialization_arrays.npz",
        sample_ids=np.asarray(evaluation["sample_ids"]),
        assignments=evaluation["assignments"].astype(np.int8),
        activation_weights=evaluation["weights"],
        masks=evaluation["masks"].astype(np.int8),
        token_counts=evaluation["route"].token_counts,
        sample_counts=evaluation["route"].sample_counts,
    )
    write_json(output_dir / "specialization_report.json", report)
    return report
