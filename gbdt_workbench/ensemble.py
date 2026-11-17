from __future__ import annotations

from decimal import Decimal
from typing import Any

import numpy as np

from .precision import PrecisionContext
from .tree import traverse_tree
from .utils import digest_json


ENSEMBLE_VERSION = 1
CONTRIBUTION_VERSION = 1


def ensemble_cache_key(
    trees: list[dict[str, Any]],
    base_score: float,
    learning_rate: float,
    precision: PrecisionContext,
) -> str:
    return digest_json(
        {
            "stage": "ensemble",
            "version": ENSEMBLE_VERSION,
            "tree_indices": [tree["tree_index"] for tree in trees],
            "tree_fingerprints": [tree["content_fingerprint"] for tree in trees],
            "base_score": float(base_score),
            "learning_rate": float(learning_rate),
            "precision": precision.tier,
            "reference_precision": precision.reference_tier,
        }
    )


def contribution_cache_key(
    trees: list[dict[str, Any]],
    learning_rate: float,
    precision: PrecisionContext,
    feature_names: tuple[str, ...],
) -> str:
    return digest_json(
        {
            "stage": "contributions",
            "version": CONTRIBUTION_VERSION,
            "tree_indices": [tree["tree_index"] for tree in trees],
            "tree_fingerprints": [tree["content_fingerprint"] for tree in trees],
            "learning_rate": float(learning_rate),
            "precision": precision.tier,
            "reference_precision": precision.reference_tier,
            "feature_names": list(feature_names),
        }
    )


def score_ensemble(
    trees: list[dict[str, Any]],
    sample_bins: np.ndarray,
    base_score: float,
    learning_rate: float,
    precision: PrecisionContext,
) -> dict[str, Any]:
    rows = int(sample_bins.shape[0])
    ordered_trees = sorted(trees, key=lambda tree: int(tree["tree_index"]))
    raw = np.full(rows, precision.cast(base_score), dtype=precision.dtype)
    learning_rate_value = precision.cast(learning_rate)
    tree_predictions: list[np.ndarray] = []
    for tree in ordered_trees:
        prediction = np.empty(rows, dtype=precision.dtype)
        for row_index in range(rows):
            leaf, _ = traverse_tree(tree, sample_bins[row_index])
            prediction[row_index] = precision.cast(leaf["value"])
        scaled = precision.cast(prediction * learning_rate_value)
        raw = precision.cast(raw + scaled)
        tree_predictions.append(prediction)
    probabilities = precision.sigmoid(raw)
    fingerprint = digest_json(
        {
            "version": ENSEMBLE_VERSION,
            "tree_indices": [tree["tree_index"] for tree in ordered_trees],
            "base_score": float(base_score),
            "learning_rate": float(learning_rate),
            "precision": precision.tier,
            "raw_scores": raw,
            "probabilities": probabilities,
        }
    )
    return {
        "version": ENSEMBLE_VERSION,
        "cache_key": ensemble_cache_key(
            ordered_trees,
            base_score,
            learning_rate,
            precision,
        ),
        "fingerprint": fingerprint,
        "tree_indices": [tree["tree_index"] for tree in ordered_trees],
        "tree_fingerprints": [tree["content_fingerprint"] for tree in ordered_trees],
        "base_score": float(base_score),
        "learning_rate": float(learning_rate),
        "precision": precision.tier,
        "raw_scores": raw,
        "probabilities": probabilities,
    }


def compute_contributions(
    trees: list[dict[str, Any]],
    sample_bins: np.ndarray,
    learning_rate: float,
    precision: PrecisionContext,
    feature_names: tuple[str, ...],
) -> dict[str, Any]:
    rows = int(sample_bins.shape[0])
    feature_count = len(feature_names)
    ordered_trees = sorted(trees, key=lambda tree: int(tree["tree_index"]))
    learning_rate_value = precision.cast(learning_rate)
    tree_vectors = np.zeros(
        (rows, len(ordered_trees), feature_count),
        dtype=precision.dtype,
    )
    for tree_position, tree in enumerate(ordered_trees):
        for row_index in range(rows):
            leaf, path_features = traverse_tree(tree, sample_bins[row_index])
            scaled_leaf = precision.cast(
                precision.cast(leaf["value"]) * learning_rate_value
            )
            if not path_features:
                continue
            share = precision.cast(
                scaled_leaf / precision.cast(len(path_features))
            )
            for feature_index in path_features:
                current = tree_vectors[row_index, tree_position, feature_index]
                tree_vectors[row_index, tree_position, feature_index] = precision.cast(
                    current + share
                )

    summary = np.zeros((rows, feature_count), dtype=precision.dtype)
    for tree_position in range(len(ordered_trees)):
        summary = precision.cast(summary + tree_vectors[:, tree_position, :])
    fingerprint = digest_json(
        {
            "version": CONTRIBUTION_VERSION,
            "tree_indices": [tree["tree_index"] for tree in ordered_trees],
            "learning_rate": float(learning_rate),
            "precision": precision.tier,
            "feature_names": list(feature_names),
            "tree_vectors": tree_vectors,
            "summary": summary,
        }
    )
    return {
        "version": CONTRIBUTION_VERSION,
        "cache_key": contribution_cache_key(
            ordered_trees,
            learning_rate,
            precision,
            feature_names,
        ),
        "fingerprint": fingerprint,
        "tree_indices": [tree["tree_index"] for tree in ordered_trees],
        "feature_names": list(feature_names),
        "feature_index_order": list(range(feature_count)),
        "tree_contributions": tree_vectors,
        "feature_contributions": summary,
    }


def reference_ensemble_scores(
    trees: list[dict[str, Any]],
    sample_bins: np.ndarray,
    reference_base_score: float,
    learning_rate: float,
    precision: PrecisionContext,
) -> list[float]:
    """Reference-tier raw scores, evaluated at fixed tree order and paths."""
    rows = int(sample_bins.shape[0])
    ordered_trees = sorted(trees, key=lambda tree: int(tree["tree_index"]))
    full_precision = precision.reference_kind == "decimal"
    if full_precision:
        accumulators = [
            Decimal.from_float(float(reference_base_score)) for _ in range(rows)
        ]
    else:
        accumulators = [np.float64(reference_base_score) for _ in range(rows)]

    for tree in ordered_trees:
        diagnostics = {item["node_id"]: item for item in tree["node_diagnostics"]}
        for row_index in range(rows):
            leaf, _ = traverse_tree(tree, sample_bins[row_index])
            leaf_diagnostic = diagnostics[int(leaf["node_id"])]
            reference_leaf = float(leaf_diagnostic["residual_mean_reference"])
            scaled = precision.reference_scale_exact(reference_leaf, learning_rate)
            accumulators[row_index] = precision.reference_add(accumulators[row_index], scaled)
    return [precision.trace_float(value) for value in accumulators]


def contributions_as_lists(contributions: dict[str, Any]) -> dict[str, Any]:
    return {
        "tree_indices": contributions["tree_indices"],
        "feature_names": contributions["feature_names"],
        "feature_index_order": contributions["feature_index_order"],
        "tree_contributions": contributions["tree_contributions"].tolist(),
        "feature_contributions": contributions["feature_contributions"].tolist(),
    }

