from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import ExperimentConfig
from .data import Dataset
from .errors import TreeTrainingError
from .precision import PrecisionContext
from .utils import digest_json, stable_seed


TREE_VERSION = 1
RESIDUAL_MODE = "parallel_initial_binary_residual"
TIE_BREAK = "feature_index_then_bin_threshold"


def tree_cache_key(
    config: ExperimentConfig,
    tree_index: int,
    binning_fingerprint: str,
    residual_fingerprint: str,
    precision: PrecisionContext,
) -> str:
    return digest_json(
        {
            "stage": "tree",
            "version": TREE_VERSION,
            "tree_index": int(tree_index),
            "binning_fingerprint": binning_fingerprint,
            "residual_fingerprint": residual_fingerprint,
            "seed": config.seed,
            "max_depth": config.max_depth,
            "row_subsample": config.row_subsample,
            "col_subsample": config.col_subsample,
            "min_samples_leaf": config.min_samples_leaf,
            "precision": precision.tier,
            "reference_precision": precision.reference_tier,
            "residual_mode": RESIDUAL_MODE,
            "tree_faulted": int(tree_index) in config.fault_tree_indices,
        }
    )


def residual_fingerprint(
    residual: np.ndarray,
    reference_residual: np.ndarray,
    precision: PrecisionContext,
) -> str:
    base_bytes = np.ascontiguousarray(residual.astype(">f4" if precision.tier == "float32" else ">f8"))
    reference_bytes = np.ascontiguousarray(reference_residual.astype(">f8"))
    return digest_json(
        {
            "precision": precision.tier,
            "reference_precision": precision.reference_tier,
            "base_sha256": base_bytes.tobytes().hex(),
            "reference_sha256": reference_bytes.tobytes().hex(),
        }
    )


def _sample_rows(config: ExperimentConfig, tree_index: int, rows: int) -> np.ndarray:
    generator = np.random.Generator(
        np.random.PCG64(stable_seed(config.seed, "tree", tree_index, "rows"))
    )
    count = max(1, int(math.floor(float(config.row_subsample) * rows)))
    count = min(count, rows)
    return np.sort(generator.choice(rows, size=count, replace=False).astype(np.int64))


def _sample_features(config: ExperimentConfig, tree_index: int, feature_count: int) -> np.ndarray:
    generator = np.random.Generator(
        np.random.PCG64(stable_seed(config.seed, "tree", tree_index, "features"))
    )
    count = max(1, int(math.floor(float(config.col_subsample) * feature_count)))
    count = min(count, feature_count)
    return np.sort(generator.choice(feature_count, size=count, replace=False).astype(np.int64))


def train_tree(
    tree_index: int,
    dataset: Dataset,
    sample_bins: np.ndarray,
    binning_fingerprint: str,
    residual: np.ndarray,
    reference_residual: np.ndarray,
    precision: PrecisionContext,
    config: ExperimentConfig,
    residual_hash: str,
) -> dict[str, Any]:
    if tree_index in config.fault_tree_indices:
        raise TreeTrainingError(
            tree_index,
            "deterministic fault injection requested by diagnostics.fault_tree_indices",
            details={"injected": True},
        )

    sampled_rows = _sample_rows(config, tree_index, dataset.rows)
    sampled_features = _sample_features(config, tree_index, dataset.feature_count)
    if len(sampled_rows) < 2 or len(sampled_features) == 0:
        raise TreeTrainingError(tree_index, "tree sample is too small to train")

    nodes: list[dict[str, Any]] = []
    node_diagnostics: list[dict[str, Any]] = []
    zero = precision.zero

    def node_stats(indices: np.ndarray) -> tuple[np.floating, float]:
        base_mean = precision.ordered_mean(residual, indices)
        reference_mean = precision.reference_mean(reference_residual, indices)
        return base_mean, reference_mean

    def find_split(indices: np.ndarray) -> dict[str, Any] | None:
        total_count = len(indices)
        if total_count < config.min_samples_leaf * 2:
            return None
        total_sum = precision.ordered_sum(residual, indices)
        best: dict[str, Any] | None = None
        best_gain = zero
        for feature_index_raw in sampled_features:
            feature_index = int(feature_index_raw)
            feature_values = sample_bins[indices, feature_index]
            unique_bins = np.unique(feature_values)
            if len(unique_bins) < 2:
                continue
            bin_counts: dict[int, int] = {}
            bin_sums: dict[int, np.floating] = {}
            for row_offset, sample_index in enumerate(indices):
                bin_value = int(feature_values[row_offset])
                bin_counts[bin_value] = bin_counts.get(bin_value, 0) + 1
                previous = bin_sums.get(bin_value, zero)
                bin_sums[bin_value] = precision.cast(
                    previous + precision.cast(residual[sample_index])
                )
            left_count = 0
            left_sum = zero
            for threshold in unique_bins[:-1].tolist():
                threshold = int(threshold)
                left_count += bin_counts[threshold]
                left_sum = precision.cast(left_sum + bin_sums[threshold])
                right_count = total_count - left_count
                if (
                    left_count < config.min_samples_leaf
                    or right_count < config.min_samples_leaf
                ):
                    continue
                right_sum = precision.cast(total_sum - left_sum)
                gain = precision.split_gain_from_sums(
                    left_sum,
                    right_sum,
                    total_sum,
                    left_count,
                    right_count,
                    total_count,
                )
                if best is None or gain > best_gain:
                    best_gain = gain
                    best = {
                        "feature_index": feature_index,
                        "bin_threshold": threshold,
                        "gain": gain,
                    }
        if best is None or best_gain <= zero:
            return None
        return best

    def build_node(indices: np.ndarray, depth: int) -> int:
        node_id = len(nodes)
        nodes.append({})
        base_mean, reference_mean = node_stats(indices)
        split = None if depth >= config.max_depth else find_split(indices)
        if split is None:
            nodes[node_id] = {
                "node_id": node_id,
                "depth": int(depth),
                "kind": "leaf",
                "sample_count": int(len(indices)),
                "value": float(base_mean),
            }
            node_diagnostics.append(
                {
                    "node_id": node_id,
                    "kind": "leaf",
                    "sample_count": int(len(indices)),
                    "residual_mean_base": float(base_mean),
                    "residual_mean_reference": float(reference_mean),
                    "split_gain_base": None,
                    "split_gain_reference": None,
                    "split_gain_abs_diff": None,
                }
            )
            return node_id

        feature_index = int(split["feature_index"])
        threshold = int(split["bin_threshold"])
        feature_values = sample_bins[indices, feature_index]
        left_mask = feature_values <= threshold
        left_indices = indices[left_mask]
        right_indices = indices[~left_mask]
        if len(left_indices) == 0 or len(right_indices) == 0:
            nodes[node_id] = {
                "node_id": node_id,
                "depth": int(depth),
                "kind": "leaf",
                "sample_count": int(len(indices)),
                "value": float(base_mean),
            }
            node_diagnostics.append(
                {
                    "node_id": node_id,
                    "kind": "leaf",
                    "sample_count": int(len(indices)),
                    "residual_mean_base": float(base_mean),
                    "residual_mean_reference": float(reference_mean),
                    "split_gain_base": None,
                    "split_gain_reference": None,
                    "split_gain_abs_diff": None,
                }
            )
            return node_id

        base_gain = precision.cast(split["gain"])
        reference_gain = precision.reference_gain(
            reference_residual,
            left_indices,
            right_indices,
        )
        left_node = build_node(left_indices, depth + 1)
        right_node = build_node(right_indices, depth + 1)
        nodes[node_id] = {
            "node_id": node_id,
            "depth": int(depth),
            "kind": "split",
            "sample_count": int(len(indices)),
            "feature_index": feature_index,
            "feature_name": dataset.feature_names[feature_index],
            "bin_threshold": threshold,
            "gain": float(base_gain),
            "left": left_node,
            "right": right_node,
        }
        node_diagnostics.append(
            {
                "node_id": node_id,
                "kind": "split",
                "sample_count": int(len(indices)),
                "residual_mean_base": float(base_mean),
                "residual_mean_reference": float(reference_mean),
                "split_gain_base": float(base_gain),
                "split_gain_reference": float(reference_gain),
                "split_gain_abs_diff": float(abs(float(base_gain) - reference_gain)),
            }
        )
        return node_id

    try:
        build_node(sampled_rows, 0)
    except TreeTrainingError:
        raise
    except Exception as exc:  # isolate one tree's failure from the ensemble
        raise TreeTrainingError(
            tree_index,
            f"tree training failed: {type(exc).__name__}: {exc}",
        ) from exc

    leaf_count = sum(1 for node in nodes if node["kind"] == "leaf")
    parameters = {
        "version": TREE_VERSION,
        "tree_index": int(tree_index),
        "seed": config.seed,
        "max_depth": config.max_depth,
        "row_subsample": config.row_subsample,
        "col_subsample": config.col_subsample,
        "min_samples_leaf": config.min_samples_leaf,
        "precision": precision.tier,
        "reference_precision": precision.reference_tier,
        "residual_mode": RESIDUAL_MODE,
        "tie_break": TIE_BREAK,
    }
    content_fingerprint = digest_json(
        {
            "parameters": parameters,
            "sampled_rows": sampled_rows,
            "sampled_features": sampled_features,
            "nodes": nodes,
            "node_diagnostics": node_diagnostics,
        }
    )
    return {
        "version": TREE_VERSION,
        "tree_index": int(tree_index),
        "stable_sort_key": [int(tree_index)],
        "cache_key": tree_cache_key(
            config,
            tree_index,
            binning_fingerprint,
            residual_hash,
            precision,
        ),
        "content_fingerprint": content_fingerprint,
        "training_residual_fingerprint": residual_hash,
        "parameters": parameters,
        "sampled_rows": sampled_rows,
        "sampled_features": sampled_features,
        "nodes": nodes,
        "node_diagnostics": node_diagnostics,
        "node_count": len(nodes),
        "leaf_count": leaf_count,
        "sample_count": int(len(sampled_rows)),
        "feature_sample_count": int(len(sampled_features)),
    }


def traverse_tree(
    tree: dict[str, Any],
    sample_bins_row: np.ndarray,
) -> tuple[dict[str, Any], list[int]]:
    nodes = tree["nodes"]
    node_id = 0
    path_features: list[int] = []
    while True:
        node = nodes[node_id]
        if node["kind"] == "leaf":
            return node, path_features
        feature_index = int(node["feature_index"])
        path_features.append(feature_index)
        if int(sample_bins_row[feature_index]) <= int(node["bin_threshold"]):
            node_id = int(node["left"])
        else:
            node_id = int(node["right"])


def tree_structure_summary(tree: dict[str, Any]) -> dict[str, Any]:
    return {
        "tree_index": tree["tree_index"],
        "stable_sort_key": tree.get("stable_sort_key", [tree["tree_index"]]),
        "content_fingerprint": tree["content_fingerprint"],
        "node_count": tree["node_count"],
        "leaf_count": tree["leaf_count"],
        "sample_count": tree["sample_count"],
        "feature_sample_count": tree["feature_sample_count"],
        "sampled_rows": tree["sampled_rows"],
        "sampled_features": tree["sampled_features"],
        "nodes": tree["nodes"],
    }
