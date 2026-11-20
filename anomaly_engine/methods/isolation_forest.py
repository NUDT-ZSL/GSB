"""Isolation-forest-style deterministic random splitting."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .common import finalize_result

EULER_GAMMA = 0.5772156649015329


def _average_path_length(size: int) -> float:
    if size <= 1:
        return 0.0
    if size == 2:
        return 1.0
    return 2.0 * (math.log(size - 1.0) + EULER_GAMMA) - 2.0 * (size - 1.0) / size


def _build_tree(
    data: np.ndarray,
    indices: np.ndarray,
    depth: int,
    max_depth: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    size = int(indices.size)
    if depth >= max_depth or size <= 1:
        return {"leaf": True, "size": size}

    node_data = data[indices]
    lows = node_data.min(axis=0)
    highs = node_data.max(axis=0)
    valid_features = np.flatnonzero(highs > lows)
    if valid_features.size == 0:
        return {"leaf": True, "size": size}

    feature = int(valid_features[int(rng.integers(0, valid_features.size))])
    low = float(lows[feature])
    high = float(highs[feature])
    threshold = low + (high - low) * float(rng.random())
    if not math.isfinite(threshold) or threshold <= low:
        threshold = float(np.nextafter(low, high))
    if threshold >= high:
        threshold = float(np.nextafter(high, low))

    left_mask = data[indices, feature] < threshold
    left_indices = indices[left_mask]
    right_indices = indices[~left_mask]
    if left_indices.size == 0 or right_indices.size == 0:
        return {"leaf": True, "size": size}

    return {
        "leaf": False,
        "feature": feature,
        "threshold": threshold,
        "size": size,
        "left": _build_tree(data, left_indices, depth + 1, max_depth, rng),
        "right": _build_tree(data, right_indices, depth + 1, max_depth, rng),
    }


def _path_length(row: np.ndarray, tree: dict[str, Any]) -> float:
    node = tree
    depth = 0
    while not node["leaf"]:
        if row[node["feature"]] < node["threshold"]:
            node = node["left"]
        else:
            node = node["right"]
        depth += 1
    return float(depth) + _average_path_length(int(node["size"]))


def score_isolation_forest(
    *,
    sample_ids: tuple[str, ...],
    data: np.ndarray,
    params: dict[str, Any],
    seed: int,
    candidate_id: str,
    row_chunk_size: int,
) -> dict[str, Any]:
    n_rows, n_features = data.shape
    n_trees = int(params["n_trees"])
    sample_size = min(int(params["sample_size"]), n_rows)
    configured_depth = int(params.get("max_depth", 0))
    max_depth = (
        configured_depth
        if configured_depth > 0
        else max(1, int(math.ceil(math.log2(max(2, sample_size)))) + 1)
    )
    rng = np.random.Generator(np.random.PCG64(seed))
    trees: list[dict[str, Any]] = []
    rng_consumption: list[dict[str, int]] = []

    for _ in range(n_trees):
        if sample_size < n_rows:
            indices = rng.choice(n_rows, size=sample_size, replace=False).astype(np.int64)
            indices.sort()
        else:
            indices = np.arange(n_rows, dtype=np.int64)
        state_before = int(rng.bit_generator.state["state"]["state"])
        trees.append(_build_tree(data, indices, 0, max_depth, rng))
        state_after = int(rng.bit_generator.state["state"]["state"])
        rng_consumption.append({"before": state_before, "after": state_after})

    path_sums = np.zeros(n_rows, dtype=np.float64)
    for tree in trees:
        for row_index in range(n_rows):
            path_sums[row_index] += _path_length(data[row_index], tree)
    mean_paths = path_sums / float(n_trees)
    denominator = _average_path_length(sample_size)
    if denominator <= 0.0:
        raw_scores = np.full(n_rows, 0.5, dtype=np.float64)
    else:
        raw_scores = np.power(2.0, -mean_paths / denominator)

    effective_params = {
        **params,
        "sample_size": sample_size,
        "effective_max_depth": max_depth,
        "seed": seed,
    }
    return finalize_result(
        method="isolation_forest",
        candidate_id=candidate_id,
        params=effective_params,
        seed=seed,
        sample_ids=sample_ids,
        raw_scores=raw_scores,
        row_chunk_size=row_chunk_size,
        algorithm_details={
            "n_trees": n_trees,
            "sample_size": sample_size,
            "effective_max_depth": max_depth,
            "n_features": n_features,
            "rng": "numpy.PCG64",
            "rng_states_sha256_input_count": len(rng_consumption),
        },
    )
