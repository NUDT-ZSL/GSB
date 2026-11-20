"""Local deterministic clustered sample generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_json_bytes, canonical_sha256, quantize_float


def _correlated_covariance(dim: int, rho: float, scale: float) -> np.ndarray:
    covariance = np.full((dim, dim), rho, dtype=np.float64)
    np.fill_diagonal(covariance, 1.0)
    covariance *= scale * scale
    return covariance


def generate_records(
    *,
    seed: int = 20261121,
    n_total: int = 600,
    n_clusters: int = 6,
    n_features: int = 5,
    anomaly_ratio: float = 0.08,
) -> list[dict[str, Any]]:
    if seed < 0:
        raise ValueError("seed must be non-negative")
    if n_total <= 1:
        raise ValueError("n_total must be greater than 1")
    if n_clusters <= 0 or n_features <= 0:
        raise ValueError("n_clusters and n_features must be positive")
    if not (0.0 < anomaly_ratio < 0.5):
        raise ValueError("anomaly_ratio must be in (0, 0.5)")

    rng = np.random.default_rng(seed)
    n_anomalies = max(1, int(round(n_total * anomaly_ratio)))
    n_normal = n_total - n_anomalies
    covariance = _correlated_covariance(n_features, 0.68, 1.35)
    centers = rng.uniform(-5.0, 5.0, size=(n_clusters, n_features))

    normal_rows: list[np.ndarray] = []
    normal_groups: list[str] = []
    base = n_normal // n_clusters
    remainder = n_normal % n_clusters
    for cluster_index in range(n_clusters):
        count = base + (1 if cluster_index < remainder else 0)
        cluster_rows = rng.multivariate_normal(
            centers[cluster_index],
            covariance,
            size=count,
            method="cholesky",
        )
        normal_rows.extend(cluster_rows)
        normal_groups.extend([f"cluster_{cluster_index:02d}"] * count)

    anomaly_rows: list[np.ndarray] = []
    anomaly_groups: list[str] = []
    magnitude_low = 7.0
    magnitude_high = 11.0
    for anomaly_index in range(n_anomalies):
        cluster_index = anomaly_index % n_clusters
        direction = rng.normal(size=n_features)
        norm = float(np.linalg.norm(direction))
        if norm == 0.0:
            direction = np.zeros(n_features, dtype=np.float64)
            direction[0] = 1.0
        else:
            direction = direction / norm
        magnitude = float(rng.uniform(magnitude_low, magnitude_high))
        anomaly_rows.append(centers[cluster_index] + direction * magnitude)
        anomaly_groups.append(f"cluster_{cluster_index:02d}")

    rows = normal_rows + anomaly_rows
    groups = normal_groups + anomaly_groups
    markers = [False] * len(normal_rows) + [True] * len(anomaly_rows)
    order = rng.permutation(len(rows))

    records: list[dict[str, Any]] = []
    for output_index, source_index in enumerate(order):
        features = [quantize_float(float(value)) for value in rows[int(source_index)]]
        records.append(
            {
                "sample_id": f"sample_{output_index:06d}",
                "feature": features,
                "group": groups[int(source_index)],
                "is_injected_anomaly": bool(markers[int(source_index)]),
            }
        )
    return records


def write_jsonl(records: list[dict[str, Any]], path: str | Path) -> str:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        for record in records:
            handle.write(canonical_json_bytes(record))
            handle.write(b"\n")
    return canonical_sha256(records)


def generate_dataset_file(
    path: str | Path,
    *,
    seed: int = 20261121,
    n_total: int = 600,
    n_clusters: int = 6,
    n_features: int = 5,
    anomaly_ratio: float = 0.08,
) -> dict[str, Any]:
    records = generate_records(
        seed=seed,
        n_total=n_total,
        n_clusters=n_clusters,
        n_features=n_features,
        anomaly_ratio=anomaly_ratio,
    )
    content_sha256 = write_jsonl(records, path)
    anomalies = sum(1 for record in records if record["is_injected_anomaly"])
    return {
        "path": str(path),
        "seed": seed,
        "n_total": len(records),
        "n_features": n_features,
        "n_clusters": n_clusters,
        "n_anomalies": anomalies,
        "actual_anomaly_ratio": round(anomalies / len(records), 12),
        "content_sha256": content_sha256,
    }


def dataset_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    feature_dims = {len(record["feature"]) for record in records}
    return {
        "n_total": len(records),
        "n_features": next(iter(feature_dims)) if len(feature_dims) == 1 else sorted(feature_dims),
        "n_anomalies": sum(1 for record in records if record["is_injected_anomaly"]),
        "sha256": canonical_sha256(records),
    }
