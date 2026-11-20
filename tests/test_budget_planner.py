from __future__ import annotations

import unittest

from anomaly_engine.budget import build_plan
from anomaly_engine.canonical import canonical_sha256
from anomaly_engine.config import normalize_config

from tests.helpers import base_config, conflict_config, make_dataset


class BudgetPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = make_dataset(n_total=300)

    def test_baseline_admits_all_and_is_deterministic(self) -> None:
        config = normalize_config(base_config(self.dataset.n_rows), n_rows=self.dataset.n_rows)
        first = build_plan(config, n_rows=self.dataset.n_rows, n_features=self.dataset.n_features)
        second = build_plan(config, n_rows=self.dataset.n_rows, n_features=self.dataset.n_features)
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))
        self.assertEqual(first["totals"]["rejected_count"], 0)
        self.assertEqual(first["totals"]["degraded_count"], 0)
        self.assertLessEqual(
            first["totals"]["estimated_comparisons"],
            config.max_comparisons,
        )
        for batch in first["execution_batches"]:
            self.assertLessEqual(
                batch["estimated_memory_bytes"],
                config.max_memory_bytes,
            )

    def test_conflict_has_deterministic_degradation_and_rejection_reasons(self) -> None:
        config = normalize_config(
            conflict_config(self.dataset.n_rows),
            n_rows=self.dataset.n_rows,
        )
        plan = build_plan(config, n_rows=self.dataset.n_rows, n_features=self.dataset.n_features)
        self.assertIn("E_MEMORY_LIMIT", plan["business_codes"])
        self.assertIn("E_COMPARE_BUDGET", plan["business_codes"])
        self.assertGreater(plan["totals"]["degraded_count"], 0)
        self.assertGreater(plan["totals"]["rejected_count"], 0)
        for decision in plan["decisions"]:
            if decision["status"] == "degraded":
                self.assertTrue(decision["degradations"])
                self.assertNotEqual(decision["original_params"], decision["effective_params"])
                self.assertTrue(
                    any(
                        reason["action"] == "degraded"
                        and reason["code"] in {"E_MEMORY_LIMIT", "E_COMPARE_BUDGET"}
                        for reason in decision["degradations"]
                    )
                )
            if decision["status"] == "rejected":
                self.assertTrue(decision["codes"])
                self.assertTrue(
                    any(reason["action"] == "rejected" for reason in decision["degradations"])
                )


if __name__ == "__main__":
    unittest.main()
