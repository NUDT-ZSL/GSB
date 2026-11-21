"""Deterministic standardization, projection, and neighborhood evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .config import method_label
from .data import Dataset
from .errors import (
    E_CONFIG_INVALID,
    E_INTERMEDIATE_DIM,
    E_RESOURCE_LIMIT,
    BusinessError,
)
from .precision import PrecisionOps, PrecisionSpec
from .serialization import array_checksum


def deterministic_projection_matrix(
    in_dim: int, out_dim: int, seed: int, density: float
) -> np.ndarray:
    """Generate a fixed-density matrix using only the configured PRNG state."""

    if in_dim <= 0 or out_dim <= 0:
        raise ValueError("projection dimensions must be positive")
    total = in_dim * out_dim
    nonzero = int(round(density * total))
    nonzero = max(1, min(total, nonzero))
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    positions = rng.permutation(total)[:nonzero]
    values = rng.standard_normal(nonzero)
    matrix = np.zeros(total, dtype=np.float64)
    matrix[positions] = values
    matrix *= 1.0 / np.sqrt(max(density * in_dim, 1.0e-12))
    return matrix.reshape(in_dim, out_dim)


def canonicalize_components(components: np.ndarray) -> np.ndarray:
    canonical = np.array(components, copy=True)
    for row_index in range(canonical.shape[0]):
        pivot = int(np.argmax(np.abs(canonical[row_index])))
        if canonical[row_index, pivot] < 0:
            canonical[row_index] *= -1
    return canonical


def _pca(
    centered: np.ndarray,
    component_count: int,
    ops: PrecisionOps,
) -> dict[str, np.ndarray]:
    if ops.mode == "fixed":
        working = ops.to_float(centered, np.float64)
        covariance = np.matmul(working.T, working) / max(1, working.shape[0] - 1)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    else:
        covariance = np.matmul(centered.T, centered) / max(1, centered.shape[0] - 1)
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(-eigenvalues, kind="stable")
    eigenvalues = np.asarray(eigenvalues[order])
    components = canonicalize_components(eigenvectors[:, order[:component_count]].T)
    positive = np.maximum(eigenvalues, 0.0)
    total = float(np.sum(positive))
    if total <= 0.0:
        ratios = np.zeros_like(positive, dtype=np.float64)
    else:
        ratios = positive.astype(np.float64) / total
    return {
        "components": ops.cast(components),
        "eigenvalues": ops.cast(eigenvalues),
        "variance_ratio": ratios.astype(np.float64),
        "covariance": covariance,
    }


def _encoded_features(
    dataset: Dataset,
    config: Mapping[str, Any],
    ops: PrecisionOps,
) -> dict[str, Any]:
    standard = ops.standardize_numeric(dataset.numeric)
    numeric_features = (
        standard["standardized"] if config["standardize"] else standard["source"]
    )
    one_hot, categorical_feature_names = ops.one_hot(
        dataset.categorical,
        dataset.categorical_columns,
        dataset.category_levels,
    )
    encoded = ops.concatenate_features(numeric_features, one_hot)
    centered, encoded_center = ops.center_columns(encoded)
    feature_names = [*dataset.numeric_columns, *categorical_feature_names]
    return {
        "encoded": encoded,
        "centered": centered,
        "encoded_center": encoded_center,
        "numeric_mean": standard["mean"],
        "numeric_scale": standard["scale"],
        "numeric_standardized": standard["standardized"],
        "numeric_source": standard["source"],
        "constant": standard["constant"],
        "feature_names": feature_names,
        "categorical_feature_names": categorical_feature_names,
    }


def _variance_spectrum(
    coordinates: np.ndarray,
    encoded: np.ndarray,
    ops: PrecisionOps,
) -> list[dict[str, float | int]]:
    coordinate_float = ops.to_float(coordinates, np.float64)
    encoded_float = ops.to_float(encoded, np.float64)
    coordinate_variance = np.var(coordinate_float, axis=0, ddof=0)
    total_variance = float(np.sum(np.var(encoded_float, axis=0, ddof=0)))
    if not np.isfinite(total_variance) or total_variance <= 0.0:
        shares = np.zeros_like(coordinate_variance)
    else:
        shares = coordinate_variance / total_variance
    cumulative = 0.0
    spectrum: list[dict[str, float | int]] = []
    for index, (variance, share) in enumerate(zip(coordinate_variance, shares)):
        cumulative += float(share)
        spectrum.append(
            {
                "dimension": index + 1,
                "coordinate_variance": float(variance),
                "variance_share": float(share),
                "cumulative_share": float(cumulative),
            }
        )
    return spectrum


def neighborhood_class_consistency(
    coordinates: np.ndarray, labels: np.ndarray, k_neighbors: int
) -> dict[str, Any]:
    coordinate_float = np.asarray(coordinates, dtype=np.float64)
    row_count = int(coordinate_float.shape[0])
    if row_count <= 1:
        return {
            "class_consistency": 1.0,
            "per_sample": [1.0] * row_count,
            "k_neighbors": 0,
            "neighbor_indices": [],
        }
    effective_k = min(int(k_neighbors), row_count - 1)
    delta = coordinate_float[:, None, :] - coordinate_float[None, :, :]
    distances = np.sum(delta * delta, axis=2)
    np.fill_diagonal(distances, np.inf)
    neighbor_indices = np.argsort(distances, axis=1, kind="stable")[:, :effective_k]
    matches = labels[neighbor_indices] == labels[:, None]
    per_sample = np.mean(matches, axis=1)
    return {
        "class_consistency": float(np.mean(per_sample)),
        "per_sample": per_sample.astype(np.float64),
        "k_neighbors": effective_k,
        "neighbor_indices": neighbor_indices.astype(np.int64),
    }


def _estimate_resources(
    *,
    method: str,
    n_rows: int,
    n_features: int,
    target_dim: int,
    intermediate_dim: int,
    bytes_per_element: int,
) -> dict[str, int]:
    coordinate_bytes = n_rows * target_dim * bytes_per_element
    if method == "pca":
        dimension_matrix = max(
            n_features * n_features,
            n_features * target_dim,
        ) * bytes_per_element
        projection_buffer = (
            n_rows * n_features * bytes_per_element
            + coordinate_bytes
        )
    elif method == "random_projection":
        dimension_matrix = n_features * target_dim * bytes_per_element
        projection_buffer = coordinate_bytes
    else:
        pca_matrix = max(
            n_features * n_features,
            n_features * intermediate_dim,
        ) * bytes_per_element
        second_matrix = intermediate_dim * target_dim * bytes_per_element
        dimension_matrix = max(pca_matrix, second_matrix)
        projection_buffer = (
            n_rows * n_features * bytes_per_element
            + n_rows * intermediate_dim * bytes_per_element
            + coordinate_bytes
        )
    return {
        "dimension_matrix_bytes": int(dimension_matrix),
        "projection_buffer_bytes": int(projection_buffer),
        "coordinate_bytes": int(coordinate_bytes),
    }


def plan_resources(
    config: Mapping[str, Any],
    dataset: Dataset,
    precision: PrecisionSpec,
) -> dict[str, Any]:
    requested = str(config["method"])
    effective = requested
    target_dim = int(config["target_dim"])
    intermediate_dim = int(config["two_stage"]["intermediate_dim"])
    resources = config["resources"]
    bytes_per = precision.bytes_per_element
    decisions: list[dict[str, Any]] = []

    if dataset.n_features > int(resources["max_features"]):
        if (
            resources["overflow_policy"] == "degrade_random_projection"
            and requested != "random_projection"
        ):
            decisions.append(
                {
                    "decision": "degrade",
                    "trigger": "max_features",
                    "field": "resources.max_features",
                    "from_method": requested,
                    "to_method": "random_projection",
                    "estimate": dataset.n_features,
                    "limit": int(resources["max_features"]),
                    "policy": resources["overflow_policy"],
                }
            )
            effective = "random_projection"
        else:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "encoded feature count exceeds the configured hard cap",
                {
                    "reason": "max_features",
                    "field": "resources.max_features",
                    "features": dataset.n_features,
                    "limit": int(resources["max_features"]),
                    "needed_bytes": dataset.n_features,
                    "overflow_policy": resources["overflow_policy"],
                },
            )

    estimates = _estimate_resources(
        method=effective,
        n_rows=dataset.n_rows,
        n_features=dataset.n_features,
        target_dim=target_dim,
        intermediate_dim=intermediate_dim,
        bytes_per_element=bytes_per,
    )

    def try_degrade(trigger: str, estimate_field: str) -> None:
        nonlocal effective, estimates
        policy = resources["overflow_policy"]
        estimate_key = (
            "dimension_matrix_bytes"
            if estimate_field == "max_dim_matrix_bytes"
            else "projection_buffer_bytes"
        )
        if policy != "degrade_random_projection" or effective == "random_projection":
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "resource estimate exceeds configured limit",
                {
                    "reason": trigger,
                    "field": f"resources.{estimate_field}",
                    "requested_method": requested,
                    "effective_method": effective,
                    "estimate": estimates[estimate_key],
                    "limit": int(resources[estimate_field]),
                },
            )
        previous = effective
        effective = "random_projection"
        decisions.append(
            {
                "decision": "degrade",
                "trigger": trigger,
                "field": f"resources.{estimate_field}",
                "from_method": previous,
                "to_method": "random_projection",
                "estimate": estimates[estimate_key],
                "limit": int(resources[estimate_field]),
                "policy": policy,
            }
        )
        estimates = _estimate_resources(
            method=effective,
            n_rows=dataset.n_rows,
            n_features=dataset.n_features,
            target_dim=target_dim,
            intermediate_dim=intermediate_dim,
            bytes_per_element=bytes_per,
        )

    limit = int(resources["max_dim_matrix_bytes"])
    if estimates["dimension_matrix_bytes"] > limit:
        try_degrade("dimension_matrix_bytes", "max_dim_matrix_bytes")
    if estimates["dimension_matrix_bytes"] > int(resources["max_dim_matrix_bytes"]):
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "degraded random-projection path still exceeds matrix memory limit",
            {
                "reason": "degraded_path_exceeds_limit",
                "field": "resources.max_dim_matrix_bytes",
                "estimate": estimates["dimension_matrix_bytes"],
                "limit": int(resources["max_dim_matrix_bytes"]),
            },
        )

    buffer_limit = int(resources["max_projection_buffer_bytes"])
    if estimates["projection_buffer_bytes"] > buffer_limit:
        try_degrade("projection_buffer_bytes", "max_projection_buffer_bytes")
    if estimates["projection_buffer_bytes"] > int(
        resources["max_projection_buffer_bytes"]
    ):
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "degraded random-projection path still exceeds projection buffer limit",
            {
                "reason": "degraded_path_exceeds_limit",
                "field": "resources.max_projection_buffer_bytes",
                "estimate": estimates["projection_buffer_bytes"],
                "limit": int(resources["max_projection_buffer_bytes"]),
            },
        )

    degradation = None
    if effective != requested:
        degradation = {
            "category": f"{requested}_to_{effective}",
            "requested_method": requested,
            "effective_method": effective,
            "requested_method_label": method_label(requested),
            "effective_method_label": method_label(effective),
            "policy": resources["overflow_policy"],
            "decisions": decisions,
        }
    return {
        "requested_method": requested,
        "effective_method": effective,
        "degraded": degradation is not None,
        "degradation": degradation,
        "limits": {
            "max_features": int(resources["max_features"]),
            "max_dim_matrix_bytes": limit,
            "max_projection_buffer_bytes": buffer_limit,
            "overflow_policy": resources["overflow_policy"],
        },
        "features": dataset.n_features,
        "rows": dataset.n_rows,
        "precision_bytes_per_element": bytes_per,
        "estimates": estimates,
        "decisions": decisions,
        "reproducible": True,
    }


@dataclass
class ExecutionResult:
    requested_method: str
    effective_method: str
    precision: PrecisionSpec
    feature_names: list[str]
    encoded: np.ndarray
    centered: np.ndarray
    encoded_center: np.ndarray
    numeric_mean: np.ndarray
    numeric_scale: np.ndarray
    numeric_standardized: np.ndarray
    constant_features: np.ndarray
    coordinates: np.ndarray
    intermediate_coordinates: np.ndarray | None
    pca_components: np.ndarray | None
    pca_eigenvalues: np.ndarray | None
    pca_variance_ratio: np.ndarray | None
    pca_stage_dim: int | None
    projection_stages: list[dict[str, Any]] = field(default_factory=list)
    fidelity_spectrum: list[dict[str, Any]] = field(default_factory=list)
    class_consistency: float = 0.0
    per_sample_consistency: np.ndarray | None = None
    neighbor_indices: np.ndarray | None = None
    variance_explained: float | None = None
    resource_plan: dict[str, Any] = field(default_factory=dict)

    def coordinate_checksum(self) -> str:
        return array_checksum(self.coordinates)

    def model_checksum(self) -> str:
        pieces = []
        if self.pca_components is not None:
            pieces.append(array_checksum(self.pca_components))
            pieces.append(array_checksum(self.pca_eigenvalues))
        for stage in self.projection_stages:
            if "matrix" in stage:
                pieces.append(array_checksum(stage["matrix"]))
        return "|".join(pieces)


def validate_runtime_dimensions(
    config: Mapping[str, Any], dataset: Dataset, method: str
) -> None:
    target = int(config["target_dim"])
    if target > dataset.n_features:
        raise BusinessError(
            E_CONFIG_INVALID,
            "target_dim exceeds the encoded feature dimension",
            {
                "field": "target_dim",
                "target_dim": target,
                "feature_dim": dataset.n_features,
            },
        )
    if method in ("pca", "pca_random_projection") and target > dataset.n_rows - 1:
        raise BusinessError(
            E_CONFIG_INVALID,
            "target_dim exceeds the available PCA rank",
            {
                "field": "target_dim",
                "target_dim": target,
                "max_pca_dim": dataset.n_rows - 1,
            },
        )
    if method == "pca_random_projection":
        intermediate = int(config["two_stage"]["intermediate_dim"])
        if intermediate < target:
            raise BusinessError(
                E_INTERMEDIATE_DIM,
                "two-stage intermediate_dim must be >= target_dim",
                {
                    "field": "two_stage.intermediate_dim",
                    "intermediate_dim": intermediate,
                    "target_dim": target,
                    "reason": "intermediate_below_target",
                },
            )
        if intermediate > dataset.n_features:
            raise BusinessError(
                E_INTERMEDIATE_DIM,
                "two-stage intermediate_dim exceeds the encoded feature dimension",
                {
                    "field": "two_stage.intermediate_dim",
                    "intermediate_dim": intermediate,
                    "feature_dim": dataset.n_features,
                    "reason": "intermediate_above_feature_dim",
                },
            )
        if intermediate > dataset.n_rows - 1:
            raise BusinessError(
                E_INTERMEDIATE_DIM,
                "two-stage intermediate_dim exceeds the available PCA rank",
                {
                    "field": "two_stage.intermediate_dim",
                    "intermediate_dim": intermediate,
                    "max_pca_dim": dataset.n_rows - 1,
                    "reason": "intermediate_above_pca_rank",
                },
            )


def execute_core(
    dataset: Dataset,
    config: Mapping[str, Any],
    *,
    method: str | None = None,
    precision: PrecisionSpec | None = None,
    resource_plan: dict[str, Any] | None = None,
) -> ExecutionResult:
    effective_method = method or (
        resource_plan["effective_method"] if resource_plan else config["method"]
    )
    chosen_precision = precision or PrecisionSpec.from_config(config)
    ops = PrecisionOps(chosen_precision)
    validate_runtime_dimensions(config, dataset, effective_method)
    features = _encoded_features(dataset, config, ops)
    target_dim = int(config["target_dim"])
    coordinate_method = effective_method
    intermediate_coordinates: np.ndarray | None = None
    pca_components: np.ndarray | None = None
    pca_eigenvalues: np.ndarray | None = None
    pca_variance_ratio: np.ndarray | None = None
    pca_stage_dim: int | None = None
    projection_stages: list[dict[str, Any]] = []
    variance_explained: float | None = None

    if coordinate_method == "pca":
        pca = _pca(features["centered"], target_dim, ops)
        pca_components = pca["components"]
        pca_eigenvalues = pca["eigenvalues"]
        pca_variance_ratio = pca["variance_ratio"]
        pca_stage_dim = target_dim
        coordinates = ops.matmul(features["centered"], pca_components.T)
        variance_explained = float(np.sum(pca_variance_ratio[:target_dim]))
        projection_stages.append(
            {
                "stage": "pca",
                "input_dim": dataset.n_features,
                "output_dim": target_dim,
                "matrix": pca_components.T,
                "matrix_checksum": array_checksum(pca_components.T),
                "parameters": {"standardize": config["standardize"]},
            }
        )
    elif coordinate_method == "random_projection":
        projection_float = deterministic_projection_matrix(
            dataset.n_features,
            target_dim,
            int(config["random_projection"]["seed"]),
            float(config["random_projection"]["density"]),
        )
        projection = ops.cast(projection_float)
        coordinates = ops.matmul(features["encoded"], projection)
        projection_stages.append(
            {
                "stage": "random_projection",
                "input_dim": dataset.n_features,
                "output_dim": target_dim,
                "matrix": projection,
                "matrix_checksum": array_checksum(projection),
                "parameters": {
                    "seed": int(config["random_projection"]["seed"]),
                    "density": float(config["random_projection"]["density"]),
                    "nnz": int(np.count_nonzero(projection_float)),
                    "scale": 1.0
                    / np.sqrt(
                        max(
                            float(config["random_projection"]["density"])
                            * dataset.n_features,
                            1.0e-12,
                        )
                    ),
                },
            }
        )
    elif coordinate_method == "pca_random_projection":
        intermediate_dim = int(config["two_stage"]["intermediate_dim"])
        pca = _pca(features["centered"], intermediate_dim, ops)
        pca_components = pca["components"]
        pca_eigenvalues = pca["eigenvalues"]
        pca_variance_ratio = pca["variance_ratio"]
        pca_stage_dim = intermediate_dim
        intermediate_coordinates = ops.matmul(features["centered"], pca_components.T)
        projection_float = deterministic_projection_matrix(
            intermediate_dim,
            target_dim,
            int(config["two_stage"]["seed"]),
            float(config["two_stage"]["density"]),
        )
        projection = ops.cast(projection_float)
        coordinates = ops.matmul(intermediate_coordinates, projection)
        variance_explained = float(np.sum(pca_variance_ratio[:intermediate_dim]))
        projection_stages.extend(
            [
                {
                    "stage": "pca",
                    "input_dim": dataset.n_features,
                    "output_dim": intermediate_dim,
                    "matrix": pca_components.T,
                    "matrix_checksum": array_checksum(pca_components.T),
                    "parameters": {"standardize": config["standardize"]},
                },
                {
                    "stage": "random_projection",
                    "input_dim": intermediate_dim,
                    "output_dim": target_dim,
                    "matrix": projection,
                    "matrix_checksum": array_checksum(projection),
                    "parameters": {
                        "seed": int(config["two_stage"]["seed"]),
                        "density": float(config["two_stage"]["density"]),
                        "nnz": int(np.count_nonzero(projection_float)),
                        "scale": 1.0
                        / np.sqrt(
                            max(
                                float(config["two_stage"]["density"])
                                * intermediate_dim,
                                1.0e-12,
                            )
                        ),
                        "intermediate_dim": intermediate_dim,
                    },
                },
            ]
        )
    else:
        raise BusinessError(
            E_CONFIG_INVALID,
            "unsupported effective method",
            {"field": "method", "value": coordinate_method},
        )

    consistency = neighborhood_class_consistency(
        ops.to_float(coordinates, np.float64),
        dataset.labels,
        int(config["evaluation"]["k_neighbors"]),
    )
    fidelity = _variance_spectrum(coordinates, features["encoded"], ops)
    result = ExecutionResult(
        requested_method=str(config["method"]),
        effective_method=coordinate_method,
        precision=chosen_precision,
        feature_names=features["feature_names"],
        encoded=features["encoded"],
        centered=features["centered"],
        encoded_center=features["encoded_center"],
        numeric_mean=features["numeric_mean"],
        numeric_scale=features["numeric_scale"],
        numeric_standardized=features["numeric_standardized"],
        constant_features=features["constant"],
        coordinates=coordinates,
        intermediate_coordinates=intermediate_coordinates,
        pca_components=pca_components,
        pca_eigenvalues=pca_eigenvalues,
        pca_variance_ratio=pca_variance_ratio,
        pca_stage_dim=pca_stage_dim,
        projection_stages=projection_stages,
        fidelity_spectrum=fidelity,
        class_consistency=float(consistency["class_consistency"]),
        per_sample_consistency=np.asarray(consistency["per_sample"], dtype=np.float64),
        neighbor_indices=np.asarray(consistency["neighbor_indices"], dtype=np.int64),
        variance_explained=variance_explained,
        resource_plan=resource_plan or {},
    )
    return result
