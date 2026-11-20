from __future__ import annotations

import unittest

from anomaly_engine.evaluation import (
    assert_result_consistency,
    compare_results,
    compute_metrics,
)
from anomaly_engine.methods import (
    finalize_result,
    score_isolation_forest,
    score_knn,
    score_mahalanobis,
)

from tests.helpers import make_dataset


class MethodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = make_dataset()
        self.injected = dict(zip(self.dataset.sample_ids, self.dataset.injected))

    def _score_all(self, chunk_size: int) -> list[dict]:
        common = {
            "sample_ids": self.dataset.sample_ids,
            "data": self.dataset.features,
            "seed": 1234,
        }
        return [
            score_isolation_forest(
                candidate_id="if-00",
                params={"n_trees": 12, "sample_size": 64, "max_depth": 0},
                row_chunk_size=chunk_size,
                **common,
            ),
            score_knn(
                candidate_id="knn-00",
                params={"n_neighbors": 5, "metric": "euclidean"},
                row_chunk_size=chunk_size,
                **common,
            ),
            score_mahalanobis(
                candidate_id="maha-00",
                params={"regularization": 1e-6, "covariance": "full"},
                row_chunk_size=chunk_size,
                **common,
            ),
        ]

    def test_repeatability_and_chunk_equivalence(self) -> None:
        chunked = self._score_all(7)
        whole = self._score_all(len(self.dataset.sample_ids))
        repeated = self._score_all(7)
        for index in range(3):
            self.assertEqual(chunked[index]["score_checksum"], whole[index]["score_checksum"])
            self.assertEqual(chunked[index]["ranking"], whole[index]["ranking"])
            self.assertEqual(chunked[index]["score_checksum"], repeated[index]["score_checksum"])
            comparison = compare_results(chunked[index], whole[index])
            self.assertTrue(comparison["consistent"])
            self.assertEqual(comparison["max_abs_delta"], 0.0)

    def test_ties_break_by_sample_id(self) -> None:
        result = finalize_result(
            method="knn",
            candidate_id="tie",
            params={},
            seed=1,
            sample_ids=("b", "a", "c"),
            raw_scores=__import__("numpy").asarray([1.0, 1.0, 1.0]),
            row_chunk_size=2,
        )
        self.assertEqual(result["ranking"], ["a", "b", "c"])

    def test_metrics_recompute_and_consistency_categories(self) -> None:
        result = self._score_all(9)[1]
        result["metrics"] = compute_metrics(
            result["score_details"],
            self.injected,
            quota_fraction=0.1,
        )
        report = assert_result_consistency(
            result,
            expected_ids=self.dataset.sample_ids,
            injected_by_id=self.injected,
            quota_fraction=0.1,
        )
        self.assertTrue(report["metrics_recomputed_equal"])
        self.assertGreaterEqual(result["metrics"]["auc"], 0.5)

        broken = dict(result)
        broken["score_details"] = result["score_details"][:-1]
        with self.assertRaises(Exception) as caught:
            assert_result_consistency(
                broken,
                expected_ids=self.dataset.sample_ids,
                injected_by_id=self.injected,
                quota_fraction=0.1,
            )
        error = caught.exception
        self.assertEqual(error.code, "E_INCONSISTENT")
        self.assertIn("sample_set_mismatch", error.details["categories"])


if __name__ == "__main__":
    unittest.main()
