from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from elasticctl.errors import BusinessError
from elasticctl.patch import parse_patch_document
from elasticctl.service import apply_patches, decide, status

from tests.common import load_fixture, sample_payload, write_json


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-validation-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _assert_invalid_load(self, **overrides: object) -> None:
        db_file = self.root / "invalid.db"
        with self.assertRaises(BusinessError) as context:
            load_fixture(db_file, **overrides)
        self.assertTrue(context.exception.code.startswith("E_"))
        self.assertFalse(db_file.exists())

    def test_duplicate_workload_name_is_rejected_without_state(self) -> None:
        workloads = copy.deepcopy(sample_payload("workloads.json"))
        workloads["workloads"][1]["name"] = workloads["workloads"][0]["name"]
        self._assert_invalid_load(workloads=workloads)

    def test_invalid_replica_range_is_rejected(self) -> None:
        workloads = copy.deepcopy(sample_payload("workloads.json"))
        workloads["workloads"][0]["minReplicas"] = 20
        workloads["workloads"][0]["currentReplicas"] = 20
        self._assert_invalid_load(workloads=workloads)

    def test_non_positive_cpu_is_rejected(self) -> None:
        workloads = copy.deepcopy(sample_payload("workloads.json"))
        workloads["workloads"][0]["cpuMillicoresPerReplica"] = 0
        self._assert_invalid_load(workloads=workloads)

    def test_invalid_percentile_is_rejected(self) -> None:
        metrics = copy.deepcopy(sample_payload("metric_sources.json"))
        metrics["metricSources"][1]["percentile"] = "1.5"
        self._assert_invalid_load(metrics=metrics)

    def test_missing_metric_reference_is_rejected(self) -> None:
        rules = copy.deepcopy(sample_payload("scaling_rules.json"))
        rules["scalingRules"][0]["metricSourceId"] = "ms-missing"
        self._assert_invalid_load(rules=rules)

    def test_duplicate_priority_is_rejected(self) -> None:
        rules = copy.deepcopy(sample_payload("scaling_rules.json"))
        rules["scalingRules"][1]["priority"] = rules["scalingRules"][0]["priority"]
        self._assert_invalid_load(rules=rules)

    def test_invalid_freeze_window_is_rejected(self) -> None:
        budget = copy.deepcopy(sample_payload("resource_budget.json"))
        budget["resourceBudget"]["freezeWindows"][0]["endMs"] = (
            budget["resourceBudget"]["freezeWindows"][0]["startMs"]
        )
        self._assert_invalid_load(budget=budget)

    def test_invalid_cooldown_is_rejected(self) -> None:
        rules = copy.deepcopy(sample_payload("scaling_rules.json"))
        rules["scalingRules"][0]["cooldownMs"] = 0
        self._assert_invalid_load(rules=rules)

    def test_non_positive_percent_is_rejected(self) -> None:
        rules = copy.deepcopy(sample_payload("scaling_rules.json"))
        rules["scalingRules"][0]["percent"] = "0"
        self._assert_invalid_load(rules=rules)

    def test_invalid_patch_does_not_change_state(self) -> None:
        db_file = self.root / "patch-invalid.db"
        load_fixture(db_file)
        decide(db_file, at=120000)
        before = status(db_file)
        patch = {
            "schemaVersion": 1,
            "patchId": "invalid-patch",
            "baseConfigVersion": 1,
            "patches": [
                {
                    "operationId": "delete-used-metric",
                    "op": "delete",
                    "objectType": "metric_source",
                    "objectId": "ms-queue-depth",
                    "expectedVersion": 1,
                }
            ],
        }
        patch_path = self.root / "invalid.patch.json"
        write_json(patch_path, patch)
        with self.assertRaises(BusinessError) as context:
            apply_patches(db_file, patch_path)
        self.assertEqual(context.exception.code, "E_MISSING_METRIC_REF")
        after = status(db_file)
        self.assertEqual(before, after)

    def test_bad_patch_document_is_rejected_before_database_access(self) -> None:
        with self.assertRaises(BusinessError) as context:
            parse_patch_document({"patchId": "x", "patches": [{"op": "modify"}]})
        self.assertEqual(context.exception.code, "E_INVALID_PATCH")


if __name__ == "__main__":
    unittest.main()

