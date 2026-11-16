"""Dimensionality reduction paths: PCA, truncated random projection, variance filter."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .encoder import EncodedMatrix
from .errors import BenchError, E_CONSTANT_COLUMN
from .hashing import digest_iterable, fingerprint


@dataclass
class ReductionResult:
    scale_id: str
    method: str
    reduced: np.ndarray
    retained_dim: int
    retained_features: list[str]
    summary: dict
    reduction_fingerprint: str
    dropped_constant_features: list[str]


def _active_indices(encoded: EncodedMatrix) -> np.ndarray:
    constant = set(encoded.constant_features)
    indices = np.array([idx for idx, name in enumerate(encoded.feature_names)
                        if name not in constant], dtype=np.int64)
    if indices.size == 0:
        raise BenchError(
            E_CONSTANT_COLUMN,
            f"scale '{encoded.scale_id}': every feature is constant; "
            "reduction cannot proceed",
        )
    return indices


def reduce_pca(config: Config, encoded: EncodedMatrix) -> ReductionResult:
    active = _active_indices(encoded)
    dropped = [encoded.feature_names[idx]
               for idx in range(len(encoded.feature_names)) if idx not in set(active)]
    matrix = encoded.matrix[:, active]
    n_samples, n_features = matrix.shape
    target = min(config.reduction.target_dim, n_features, max(1, n_samples - 1))
    mean = matrix.mean(axis=0)
    centered = matrix - mean
    u, singular_values, vt = np.linalg.svd(centered, full_matrices=False)
    components = vt[:target].copy()
    for idx in range(components.shape[0]):
        pivot = int(np.argmax(np.abs(components[idx])))
        if components[idx, pivot] < 0:
            components[idx] *= -1.0
    reduced = centered @ components.T
    eigenvalues = singular_values[:target] ** 2 / max(1, n_samples - 1)
    total_variance = float((centered ** 2).sum() / max(1, n_samples - 1))
    explained_ratio = (eigenvalues / total_variance).tolist() if total_variance > 0 \
        else [0.0] * target
    retained_features = [f"pca_component_{idx}" for idx in range(target)]
    summary = {
        "explained_variance": [float(value) for value in eigenvalues],
        "explained_variance_ratio": [float(value) for value in explained_ratio],
        "cumulative_explained_variance_ratio": np.cumsum(explained_ratio).tolist(),
        "dropped_constant_count": len(dropped),
        "active_input_features": int(n_features),
    }
    reduction_fingerprint = _fingerprint(config, encoded, "pca", target, summary)
    return ReductionResult(encoded.scale_id, "pca", reduced, target,
                           retained_features, summary, reduction_fingerprint,
                           dropped)


def reduce_random_projection(config: Config, encoded: EncodedMatrix
                             ) -> ReductionResult:
    active = _active_indices(encoded)
    dropped = [encoded.feature_names[idx]
               for idx in range(len(encoded.feature_names)) if idx not in set(active)]
    matrix = encoded.matrix[:, active]
    n_samples, n_features = matrix.shape
    target = min(config.reduction.target_dim, n_features)
    seed_text = f"{config.config_fingerprint}|{encoded.scale_id}|" \
                f"{config.reduction.rp_seed_tag}"
    seed = int(fingerprint(seed_text, prefix="rp-seed:")[:16], 16)
    rng = np.random.default_rng(seed)
    projection = rng.normal(0.0, 1.0 / np.sqrt(target), size=(n_features, target))
    reduced = matrix @ projection
    column_norms = np.linalg.norm(reduced, axis=0)
    input_energy = float(np.linalg.norm(matrix, ord="fro") ** 2)
    projected_energy = float(np.linalg.norm(reduced, ord="fro") ** 2)
    summary = {
        "projection_seed": seed,
        "projection_shape": [n_features, target],
        "column_l2_norm": [float(value) for value in column_norms],
        "frobenius_energy_ratio":
            projected_energy / input_energy if input_energy > 0 else 0.0,
        "dropped_constant_count": len(dropped),
        "active_input_features": int(n_features),
    }
    retained_features = [f"rp_component_{idx}" for idx in range(target)]
    reduction_fingerprint = _fingerprint(config, encoded, "random_projection",
                                         target, summary)
    return ReductionResult(encoded.scale_id, "random_projection", reduced, target,
                           retained_features, summary, reduction_fingerprint,
                           dropped)


def reduce_variance(config: Config, encoded: EncodedMatrix) -> ReductionResult:
    active = _active_indices(encoded)
    active_set = set(active.tolist())
    dropped = [encoded.feature_names[idx]
               for idx in range(len(encoded.feature_names)) if idx not in active_set]
    matrix = encoded.matrix[:, active]
    variances = matrix.var(axis=0, ddof=1) if matrix.shape[0] > 1 else \
        matrix.var(axis=0)
    threshold = config.reduction.variance_threshold
    eligible = np.flatnonzero(variances >= threshold)
    ordered = eligible[np.argsort(-variances[eligible], kind="stable")]
    target = min(config.reduction.target_dim, ordered.size)
    chosen_local = np.sort(ordered[:target])
    chosen = active[chosen_local]
    reduced = encoded.matrix[:, chosen]
    retained_features = [encoded.feature_names[int(idx)] for idx in chosen]
    summary = {
        "variance_threshold": float(threshold),
        "eligible_feature_count": int(eligible.size),
        "retained_variance": [float(variances[int(idx)])
                              for idx in chosen_local],
        "dropped_below_threshold": int(max(0, variances.size - eligible.size)),
        "dropped_constant_count": len(dropped),
        "active_input_features": int(matrix.shape[1]),
    }
    reduction_fingerprint = _fingerprint(config, encoded, "variance", target,
                                         summary)
    return ReductionResult(encoded.scale_id, "variance", reduced, target,
                           retained_features, summary, reduction_fingerprint,
                           dropped)


REDUCERS = {
    "pca": reduce_pca,
    "random_projection": reduce_random_projection,
    "variance": reduce_variance,
}


def _fingerprint(config: Config, encoded: EncodedMatrix, method: str,
                 target: int, summary: dict) -> str:
    return digest_iterable([
        config.config_fingerprint, encoded.encoder_fingerprint,
        method, target, summary,
    ])
