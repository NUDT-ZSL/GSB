"""Stage 3: deterministic PCA, random projections and decayed covariance."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import DiagnosticError, intermediate_dim
from .precision import Precision


def get_active_candidate(config: dict[str, Any]) -> dict[str, Any]:
    reduction_cfg = config.get("reduction", {})
    active = reduction_cfg.get("active")
    for candidate in reduction_cfg.get("candidates", []):
        if candidate.get("name") == active:
            return dict(candidate)
    raise DiagnosticError("E_BAD_INPUT", "active reduction candidate is not declared", active=active)


def _sign_correct(components: np.ndarray) -> np.ndarray:
    corrected = components.copy()
    for col in range(corrected.shape[1]):
        vector = corrected[:, col]
        max_index = int(np.argmax(np.abs(vector)))
        if vector[max_index] < 0:
            corrected[:, col] = -vector
    return corrected


def fit_pca(features: np.ndarray, dim: int, precision: Precision) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows, cols = features.shape
    max_dim = min(rows, cols)
    if dim <= 0 or dim > max_dim:
        raise intermediate_dim("PCA dimension is outside matrix rank", dim=dim, max_dim=max_dim)
    mean = precision.mean(features, axis=0)
    centered = precision.sub(features, mean)
    _, singular_values, vt = np.linalg.svd(centered.astype(np.float64), full_matrices=False)
    components = _sign_correct(vt[:dim].T)
    projection = precision.q(components)
    coordinates = precision.matmul(precision.sub(features, mean), projection)
    explained = precision.q((singular_values[:dim] ** 2) / max(1, rows - 1))
    return projection, coordinates, explained


def make_random_projection(
    input_dim: int,
    output_dim: int,
    density: float,
    seed: int,
    precision: Precision,
) -> np.ndarray:
    if output_dim <= 0 or input_dim <= 0:
        raise intermediate_dim("random projection dimensions must be positive", input_dim=input_dim, output_dim=output_dim)
    if not 0.0 < density <= 1.0:
        raise DiagnosticError("E_BAD_INPUT", "random projection density must be in (0, 1]", density=density)
    rng = np.random.default_rng(int(seed))
    sparse_mask = rng.random((input_dim, output_dim)) < density
    signs = rng.choice(np.asarray([-1.0, 1.0]), size=(input_dim, output_dim))
    scale = 1.0 / math.sqrt(max(density * input_dim, 1.0))
    matrix = signs * sparse_mask.astype(np.float64) * scale
    return precision.q(matrix)


def decay_weights(row_count: int, decay: float, precision: Precision) -> np.ndarray:
    if not 0.0 <= decay <= 1.0:
        raise DiagnosticError("E_BAD_INPUT", "covariance decay must be in [0, 1]", decay=decay)
    ages = np.arange(row_count - 1, -1, -1, dtype=np.float64)
    weights = np.power(decay, ages)
    return precision.q(weights)


def decayed_covariance_diagonal(
    coordinates: np.ndarray,
    decay_cfg: dict[str, Any],
    precision: Precision,
) -> tuple[np.ndarray, dict[str, Any]]:
    rows = coordinates.shape[0]
    decay = float(decay_cfg.get("decay", 0.9))
    weights = decay_weights(rows, decay, precision)
    weight_sum = precision.sum(weights)
    if float(weight_sum) == 0.0:
        raise DiagnosticError("E_DRIFT_MISSING", "decayed covariance weight sum is zero")
    expanded_weights = weights[:, None]
    weighted_sum = precision.sum(precision.mul(coordinates, expanded_weights), axis=0)
    mean = precision.div(weighted_sum, weight_sum)
    deviations = precision.sub(coordinates, mean)
    weighted_squares = precision.mul(precision.square(deviations), expanded_weights)
    cov_diag = precision.div(precision.sum(weighted_squares, axis=0), weight_sum)
    meta = {
        "decay": decay,
        "ddof": int(decay_cfg.get("ddof", 0)),
        "weight_sum": float(weight_sum),
        "weights": [float(x) for x in weights],
        "mean": [float(x) for x in mean],
    }
    return precision.q(cov_diag), meta


def reduce_features(
    standardize_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    candidate = get_active_candidate(config)
    method = candidate["method"]
    output_dim = int(candidate["dim"])
    features = np.asarray(standardize_result["features"], dtype=np.float64)
    rows, cols = features.shape

    result: dict[str, Any] = {
        "window": standardize_result["window"],
        "v_idx": standardize_result["v_idx"],
        "candidate": candidate["name"],
        "method": method,
        "output_dim": output_dim,
        "precision": precision.config_view(),
    }

    if method == "pca":
        projection, coordinates, explained = fit_pca(features, output_dim, precision)
        result.update(
            {
                "projection": projection,
                "coordinates": coordinates,
                "explained_variance": explained,
                "projection_layers": [{"name": "pca", "matrix": projection}],
            }
        )
    elif method == "random_projection":
        density = float(candidate.get("density", 1.0))
        projection = make_random_projection(cols, output_dim, density, int(candidate.get("seed", 0)), precision)
        coordinates = precision.matmul(features, projection)
        result.update(
            {
                "projection": projection,
                "coordinates": coordinates,
                "density": density,
                "actual_density": float(np.count_nonzero(projection) / projection.size),
                "projection_layers": [{"name": "random_projection", "matrix": projection}],
            }
        )
    elif method == "pca_then_random_projection":
        middle = candidate.get("intermediate_dim")
        max_dim = min(rows, cols)
        if not isinstance(middle, int) or middle <= output_dim or middle > max_dim:
            raise intermediate_dim(
                "nested intermediate dimension must satisfy output_dim < intermediate_dim <= matrix rank",
                candidate=candidate["name"],
                intermediate_dim=middle,
                output_dim=output_dim,
                max_dim=max_dim,
            )
        pca_matrix, intermediate, explained = fit_pca(features, middle, precision)
        density = float(candidate.get("density", 1.0))
        rp_matrix = make_random_projection(
            middle, output_dim, density, int(candidate.get("seed", 0)) + 1009, precision
        )
        coordinates = precision.matmul(intermediate, rp_matrix)
        result.update(
            {
                "projection": pca_matrix,
                "random_projection": rp_matrix,
                "intermediate_coordinates": intermediate,
                "intermediate_dim": middle,
                "explained_variance": explained,
                "coordinates": coordinates,
                "density": density,
                "actual_density": float(np.count_nonzero(rp_matrix) / rp_matrix.size),
                "projection_layers": [
                    {"name": "pca", "matrix": pca_matrix, "intermediate_dim": middle},
                    {"name": "random_projection", "matrix": rp_matrix},
                ],
            }
        )
    else:
        raise DiagnosticError("E_BAD_INPUT", "unsupported reduction method", method=method)

    cov_diag, covariance_meta = decayed_covariance_diagonal(
        result["coordinates"], config.get("covariance", {}), precision
    )
    result["covariance_diag"] = cov_diag
    result["covariance_meta"] = covariance_meta
    return result
