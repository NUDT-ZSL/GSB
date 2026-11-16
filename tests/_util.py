"""Shared helpers for the featbench test suite."""
from __future__ import annotations

import os
import shutil
import tempfile

import yaml

BASE_CONFIG = {
    "experiment": "unit_experiment",
    "seed": 4242,
    "workers": 1,
    "impute_missing": True,
    "columns": [
        {"name": "num_a", "type": "numeric", "missing_rate": 0.1},
        {"name": "num_b", "type": "numeric"},
        {"name": "num_red", "type": "numeric_redundant",
         "redundant_of": "num_a"},
        {"name": "cat_low", "type": "categorical_low", "base_cardinality": 4,
         "missing_rate": 0.1},
        {"name": "cat_high", "type": "categorical_high",
         "base_cardinality": 30},
        {"name": "target", "type": "numeric"},
    ],
    "target_column": "target",
    "scales": [
        {"id": "s1", "samples": 80, "feature_multiplier": 1.0,
         "cardinality_multiplier": 1.0},
    ],
    "cross": {
        "min_order": 1,
        "max_order": 2,
        "whitelist": ["cat_low", "cat_high"],
        "cardinality_budget": 16,
        "degrade_strategy": "hash_bucket",
    },
    "reduction": {
        "methods": ["variance", "pca", "random_projection"],
        "target_dim": 8,
        "variance_threshold": 0.01,
    },
}


def write_config(tmp_dir: str, payload: dict, name: str = "config.yaml") -> str:
    path = os.path.join(tmp_dir, name)
    body = dict(BASE_CONFIG)
    body.update(payload)
    body["work_dir"] = os.path.join(tmp_dir, "work")
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(body, handle, sort_keys=True)
    return path


class TempWorkdir:
    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="featbench-test-")

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)
