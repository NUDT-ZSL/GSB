from __future__ import annotations

import copy

from anomaly_engine.dataset import generate_records
from anomaly_engine.validation import Dataset, validate_samples


def make_dataset(n_total: int = 180, seed: int = 20261121) -> Dataset:
    records = generate_records(
        seed=seed,
        n_total=n_total,
        n_clusters=5,
        n_features=4,
        anomaly_ratio=0.1,
    )
    return validate_samples(records, source_sha256="test-fixture")


def base_config(n_total: int) -> dict:
    return {
        "run_seed": 20261121,
        "methods": {
            "isolation_forest": [
                {"n_trees": 16, "sample_size": min(96, n_total), "max_depth": 0},
                {"n_trees": 32, "sample_size": min(128, n_total), "max_depth": 0},
            ],
            "knn": [
                {"n_neighbors": 3},
                {"n_neighbors": 7},
            ],
            "mahalanobis": [
                {"regularization": 0.000001},
            ],
        },
        "parallel": {"workers": 2, "batch_size": 32, "start_method": "spawn"},
        "budgets": {"max_comparisons": 50_000_000, "max_memory_bytes": 64_000_000},
        "priority": {"order": ["quality", "memory", "progress"]},
        "evaluation": {"quota_fraction": 0.1},
    }


def conflict_config(n_total: int) -> dict:
    config = copy.deepcopy(base_config(n_total))
    config["budgets"] = {
        "max_comparisons": 90_000,
        "max_memory_bytes": 120_000,
    }
    return config
