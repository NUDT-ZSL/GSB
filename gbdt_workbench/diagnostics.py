from __future__ import annotations

import math
from typing import Any

import numpy as np

from .ensemble import reference_ensemble_scores
from .precision import PrecisionContext


ERROR_STAGE_ORDER = (
    "split_gain",
    "leaf_value",
    "learning_rate_scale",
    "ensemble_sum",
)


def _difference(base: float, reference: float) -> float:
    base_value = float(base)
    reference_value = float(reference)
    if not math.isfinite(base_value) or not math.isfinite(reference_value):
        return math.inf
    return abs(base_value - reference_value)


def _item(
    stage: str,
    base: float,
    reference: float,
    tolerance: float,
    **identity: Any,
) -> dict[str, Any]:
    diff = _difference(base, reference)
    item = {
        "stage": stage,
        **identity,
        "base": float(base),
        "reference": float(reference),
        "abs_diff": float(diff),
        "exceeds_tolerance": bool(diff > tolerance),
    }
    return item


def trace_error_propagation(
    trees: list[dict[str, Any]],
    sample_bins: np.ndarray,
    ensemble: dict[str, Any],
    contributions: dict[str, Any],
    precision: PrecisionContext,
    reference_base_score: float,
    learning_rate: float,
    tolerance: float,
    decision_threshold: float,
) -> dict[str, Any]:
    ordered_trees = sorted(trees, key=lambda tree: int(tree["tree_index"]))
    stages: dict[str, list[dict[str, Any]]] = {
        stage: [] for stage in ERROR_STAGE_ORDER
    }

    for tree in ordered_trees:
        tree_index = int(tree["tree_index"])
        for diagnostic in sorted(tree["node_diagnostics"], key=lambda item: int(item["node_id"])):
            node_id = int(diagnostic["node_id"])
            if diagnostic["kind"] == "split":
                stages["split_gain"].append(
                    _item(
                        "split_gain",
                        diagnostic["split_gain_base"],
                        diagnostic["split_gain_reference"],
                        tolerance,
                        tree_index=tree_index,
                        node_id=node_id,
                    )
                )
            else:
                stages["leaf_value"].append(
                    _item(
                        "leaf_value",
                        diagnostic["residual_mean_base"],
                        diagnostic["residual_mean_reference"],
                        tolerance,
                        tree_index=tree_index,
                        node_id=node_id,
                    )
                )
                stages["learning_rate_scale"].append(
                    _item(
                        "learning_rate_scale",
                        precision.cast(
                            precision.cast(diagnostic["residual_mean_base"])
                            * precision.cast(learning_rate)
                        ),
                        precision.reference_scale(
                            float(diagnostic["residual_mean_reference"]),
                            learning_rate,
                        ),
                        tolerance,
                        tree_index=tree_index,
                        node_id=node_id,
                    )
                )

    reference_raw = reference_ensemble_scores(
        ordered_trees,
        sample_bins,
        reference_base_score,
        learning_rate,
        precision,
    )
    base_raw = np.asarray(ensemble["raw_scores"], dtype=precision.dtype)
    for sample_index in range(len(reference_raw)):
        stages["ensemble_sum"].append(
            _item(
                "ensemble_sum",
                float(base_raw[sample_index]),
                reference_raw[sample_index],
                tolerance,
                sample_index=sample_index,
            )
        )

    first_item: dict[str, Any] | None = None
    first_stage: str | None = None
    summaries: dict[str, Any] = {}
    for stage in ERROR_STAGE_ORDER:
        items = stages[stage]
        exceeding = [item for item in items if item["exceeds_tolerance"]]
        max_diff = max((item["abs_diff"] for item in items), default=0.0)
        summaries[stage] = {
            "item_count": len(items),
            "exceed_count": len(exceeding),
            "max_abs_diff": float(max_diff),
            "first_exceeding_item": exceeding[0] if exceeding else None,
        }
        if first_item is None and exceeding:
            first_item = exceeding[0]
            first_stage = stage

    base_probabilities = np.asarray(ensemble["probabilities"], dtype=precision.dtype)
    reference_probabilities = [
        precision.reference_sigmoid(value) for value in reference_raw
    ]
    contribution_rows = np.asarray(contributions["feature_contributions"])
    feature_names = list(contributions["feature_names"])
    sensitive: list[dict[str, Any]] = []
    for sample_index, (base_probability, reference_probability) in enumerate(
        zip(base_probabilities.tolist(), reference_probabilities, strict=True)
    ):
        base_positive = float(base_probability) >= decision_threshold
        reference_positive = float(reference_probability) >= decision_threshold
        if base_positive == reference_positive:
            continue
        row = contribution_rows[sample_index]
        nonzero_indices = [index for index, value in enumerate(row) if float(value) != 0.0]
        if nonzero_indices:
            dominant_index = min(
                nonzero_indices,
                key=lambda index: (-abs(float(row[index])), index),
            )
            dominant_feature: str | None = feature_names[dominant_index]
            dominant_contribution = float(row[dominant_index])
        else:
            dominant_feature = None
            dominant_contribution = 0.0
        sensitive.append(
            {
                "sample_index": sample_index,
                "base_probability": float(base_probability),
                "reference_probability": float(reference_probability),
                "probability_delta": float(reference_probability - base_probability),
                "decision_threshold": float(decision_threshold),
                "base_decision": int(base_positive),
                "reference_decision": int(reference_positive),
                "dominant_feature": dominant_feature,
                "dominant_contribution": dominant_contribution,
            }
        )

    error_attribution = {
        "base_precision": precision.tier,
        "reference_precision": precision.reference_tier,
        "tolerance": float(tolerance),
        "stage_order": list(ERROR_STAGE_ORDER),
        "first_exceeding_stage": first_stage,
        "first_exceeding_item": first_item,
        "stage_summaries": summaries,
        "items": stages,
    }
    return {
        "error_attribution": error_attribution,
        "threshold_sensitive_samples": sensitive,
    }
