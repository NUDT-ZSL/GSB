from __future__ import annotations

import unittest

from anomaly_engine.canonical import canonical_json_bytes, canonical_sha256
from anomaly_engine.engine import run_experiment

from tests.helpers import base_config, conflict_config, make_dataset


class IntegrationTests(unittest.TestCase):
    def test_full_experiment_deterministic_and_budget_aware(self) -> None:
        dataset = make_dataset(n_total=180)
        baseline = base_config(dataset.n_rows)
        conflict = conflict_config(dataset.n_rows)
        first = run_experiment(
            baseline_config=baseline,
            conflict_config=conflict,
            dataset=dataset,
        )
        second = run_experiment(
            baseline_config=baseline,
            conflict_config=conflict,
            dataset=dataset,
        )
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))

        self.assertTrue(first["scenarios"]["baseline"]["ok"])
        self.assertTrue(first["scenarios"]["baseline"]["consistency"]["parallel_serial_all_equal"])
        self.assertTrue(first["scenarios"]["baseline"]["consistency"]["chunked_whole_all_equal"])
        self.assertEqual(
            first["snapshots"]["current_versions"],
            {"plan": 2, "budget": 2, "result": 2},
        )
        conflict_report = first["scenarios"]["budget_conflict"]
        self.assertIn("E_COMPARE_BUDGET", conflict_report["business_codes"])
        self.assertTrue(conflict_report["scheduling_report"]["degraded_task_ids"])
        self.assertTrue(conflict_report["scheduling_report"]["rejected_task_ids"])
        self.assertTrue(conflict_report["consistency"]["parallel_serial_all_equal"])
        for deviation in conflict_report["deviation_spectrum"]:
            self.assertEqual(deviation["max_abs_delta"], 0.0)
            self.assertIsNone(deviation["first_score_mismatch"])


if __name__ == "__main__":
    unittest.main()
