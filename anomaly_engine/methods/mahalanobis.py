"""Full covariance Mahalanobis distance scoring."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..errors import EngineError
from .common import finalize_result


def score_mahalanobis(
    *,
    sample_ids: tuple[str, ...],
    data: np.ndarray,
    params: dict[str, Any],
    seed: int,
    candidate_id: str,
    row_chunk_size: int,
) -> dict[str, Any]:
    n_rows, n_features = data.shape
    if n_rows < 2:
        raise EngineError(
            "E_INSUFFICIENT_SAMPLES",
            "method_fit",
            "mahalanobis requires at least two samples",
            method="mahalanobis",
        )

    mean = data.mean(axis=0)
    covariance = np.cov(data, rowvar=False, ddof=1)
    if n_features == 1:
        covariance = np.asarray([[float(covariance)]], dtype=np.float64)
    else:
        covariance = np.asarray(covariance, dtype=np.float64)

    mean_variance = max(float(np.mean(np.diag(covariance))), 1.0)
    regularization = float(params["regularization"]) * mean_variance
    regularized = covariance + np.eye(n_features, dtype=np.float64) * regularization
    inverse_mode = "inverse"
    try:
        inverse = np.linalg.inv(regularized)
    except np.linalg.LinAlgError:
        inverse = np.linalg.pinv(regularized)
        inverse_mode = "pseudo_inverse_fallback"

    chunk_size = max(1, min(int(row_chunk_size), n_rows))
    raw_scores = np.empty(n_rows, dtype=np.float64)
    for start in range(0, n_rows, chunk_size):
        end = min(start + chunk_size, n_rows)
        for row_index in range(start, end):
            diff = data[row_index] - mean
            squared = float(np.einsum("i,ij,j->", diff, inverse, diff, optimize=False))
            raw_scores[row_index] = float(np.sqrt(max(0.0, squared)))

    return finalize_result(
        method="mahalanobis",
        candidate_id=candidate_id,
        params={
            **params,
            "effective_regularization": regularization,
            "seed": seed,
        },
        seed=seed,
        sample_ids=sample_ids,
        raw_scores=raw_scores,
        row_chunk_size=row_chunk_size,
        algorithm_details={
            "covariance": "full",
            "inverse_mode": inverse_mode,
            "n_features": n_features,
            "mean_for_scores": mean.tolist(),
        },
    )
