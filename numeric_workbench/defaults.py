"""Default local demonstration configuration."""

from __future__ import annotations

from typing import Any


def default_config_dict(job_id: str = "demo-job") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "model_family": "logistic_regression",
        "regularization_strength": 0.01,
        "max_iterations": 60,
        "learning_rate": 0.35,
        "convergence_tolerance": 1e-4,
        "min_improvement": 1e-8,
        "convergence_strategy": "tolerance_band",
        "standardization": "zscore",
        "precision": "float64",
        "fixed_scale": 65536,
        "rounding_mode": "nearest_even",
        "chunk_size": 32,
        "consistency_tolerance": 1e-6,
        "validation_fraction": 0.25,
        "subnormal_floor": 1e-300,
        "tolerance_band_factor": 1.0,
        "numeric_columns": ["num_0", "num_1", "num_2"],
        "categorical_columns": ["cat_0", "cat_1"],
        "label_column": "label",
        "positive_label": "yes",
        "reference_precision": "float64",
    }
