"""Exact brute-force K-nearest-neighbour distance scoring."""

from __future__ import annotations

from typing import Any

import numpy as np

from .common import finalize_result


def score_knn(
    *,
    sample_ids: tuple[str, ...],
    data: np.ndarray,
    params: dict[str, Any],
    seed: int,
    candidate_id: str,
    row_chunk_size: int,
) -> dict[str, Any]:
    n_rows = int(data.shape[0])
    n_neighbors = int(params["n_neighbors"])
    chunk_size = max(1, min(int(row_chunk_size), n_rows))
    raw_scores = np.empty(n_rows, dtype=np.float64)

    for start in range(0, n_rows, chunk_size):
        end = min(start + chunk_size, n_rows)
        for row_index in range(start, end):
            diff = data - data[row_index]
            squared = np.einsum("ij,ij->i", diff, diff, optimize=False)
            squared[row_index] = np.inf
            nearest = np.partition(squared, n_neighbors - 1)[:n_neighbors]
            raw_scores[row_index] = float(np.sqrt(float(nearest.mean())))

    return finalize_result(
        method="knn",
        candidate_id=candidate_id,
        params={**params, "seed": seed},
        seed=seed,
        sample_ids=sample_ids,
        raw_scores=raw_scores,
        row_chunk_size=row_chunk_size,
        algorithm_details={
            "n_neighbors": n_neighbors,
            "metric": "euclidean",
            "self_excluded": True,
            "aggregation": "mean_of_k_distances",
            "pair_order": "input_rows_sorted_by_sample_id",
        },
    )
