from __future__ import annotations

import unittest

from anomaly_engine.config import normalize_config
from anomaly_engine.dataset import generate_records
from anomaly_engine.errors import EngineError
from anomaly_engine.validation import validate_samples

from tests.helpers import base_config


class DatasetValidationTests(unittest.TestCase):
    def test_generation_is_deterministic_and_has_markers(self) -> None:
        first = generate_records(seed=7, n_total=100, n_clusters=4, n_features=3, anomaly_ratio=0.1)
        second = generate_records(seed=7, n_total=100, n_clusters=4, n_features=3, anomaly_ratio=0.1)
        self.assertEqual(first, second)
        self.assertEqual(sum(record["is_injected_anomaly"] for record in first), 10)
        self.assertEqual(len({record["sample_id"] for record in first}), 100)

    def test_duplicate_sample_id_code_and_field(self) -> None:
        records = generate_records(seed=1, n_total=20, n_clusters=2, n_features=2, anomaly_ratio=0.1)
        records[1]["sample_id"] = records[0]["sample_id"]
        with self.assertRaises(EngineError) as caught:
            validate_samples(records)
        self.assertEqual(caught.exception.code, "E_DUPLICATE_SAMPLE_ID")
        self.assertEqual(caught.exception.field, "sample_id")

    def test_feature_dimension_mismatch(self) -> None:
        records = generate_records(seed=1, n_total=20, n_clusters=2, n_features=2, anomaly_ratio=0.1)
        records[3]["feature"].append(1.0)
        with self.assertRaises(EngineError) as caught:
            validate_samples(records)
        self.assertEqual(caught.exception.code, "E_FEATURE_DIM_MISMATCH")
        self.assertEqual(caught.exception.field, "feature")

    def test_unknown_method(self) -> None:
        config = base_config(20)
        config["methods"]["not_a_method"] = [{}]
        with self.assertRaises(EngineError) as caught:
            normalize_config(config, n_rows=20)
        self.assertEqual(caught.exception.code, "E_UNKNOWN_METHOD")
        self.assertIn("methods.not_a_method", caught.exception.field)

    def test_invalid_neighbor_and_tree_counts(self) -> None:
        config = base_config(20)
        config["methods"]["knn"] = [{"n_neighbors": 0}]
        with self.assertRaises(EngineError) as caught:
            normalize_config(config, n_rows=20)
        self.assertEqual(caught.exception.code, "E_INVALID_NEIGHBORS")
        self.assertEqual(caught.exception.field, "methods.knn[0].n_neighbors")

        config = base_config(20)
        config["methods"]["isolation_forest"] = [{"n_trees": 0}]
        with self.assertRaises(EngineError) as caught:
            normalize_config(config, n_rows=20)
        self.assertEqual(caught.exception.code, "E_INVALID_TREES")
        self.assertEqual(caught.exception.field, "methods.isolation_forest[0].n_trees")

    def test_non_positive_budgets(self) -> None:
        for field in ("max_comparisons", "max_memory_bytes"):
            config = base_config(20)
            config["budgets"][field] = 0
            with self.assertRaises(EngineError) as caught:
                normalize_config(config, n_rows=20)
            self.assertEqual(caught.exception.code, "E_INVALID_BUDGET")
            self.assertIn(field, caught.exception.field)

    def test_candidate_seed_is_accepted_on_every_method(self) -> None:
        config = base_config(20)
        config["methods"]["knn"][0]["seed"] = 123
        config["methods"]["mahalanobis"][0]["seed"] = 456
        normalized = normalize_config(config, n_rows=20)
        knn_seeds = [candidate.seed for candidate in normalized.candidates if candidate.method == "knn"]
        maha_seeds = [
            candidate.seed
            for candidate in normalized.candidates
            if candidate.method == "mahalanobis"
        ]
        self.assertEqual(knn_seeds[0], 123)
        self.assertEqual(maha_seeds[0], 456)


if __name__ == "__main__":
    unittest.main()
