from __future__ import annotations

import copy
import json
import unittest

from kdrift.compare import compute_diff
from kdrift.model import EngineConfig, Resource
from tests.helpers import SAMPLES, sample_bundle


class CompareTests(unittest.TestCase):
    def test_small_sample_covers_all_drift_types_and_is_stable(self) -> None:
        desired, actual, config, _, issues = sample_bundle(SAMPLES)
        first = compute_diff(desired, actual, issues, config)
        second = compute_diff(desired, actual, issues, config)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )
        self.assertEqual(first["algorithm"]["mode"], "exact")
        self.assertEqual(first["summary"]["drift_count"], 10)
        self.assertEqual(
            first["summary"]["by_type"],
            {
                "extra": 3,
                "field_mismatch": 1,
                "missing": 4,
                "replicas_mismatch": 1,
                "selector_mismatch": 1,
            },
        )

    def test_scale_sample_switches_to_digest_with_equivalent_results(
        self,
    ) -> None:
        desired, actual, config, _, issues = sample_bundle(
            SAMPLES / "scale"
        )
        result = compute_diff(desired, actual, issues, config)
        self.assertEqual(result["algorithm"]["mode"], "digest_sampled")
        self.assertTrue(result["algorithm"]["triggers"])
        self.assertTrue(result["equivalence"]["required"])
        self.assertTrue(result["equivalence"]["equivalent"])
        self.assertEqual(result["summary"]["drift_count"], 10)
        changed = [
            evidence
            for evidence in result["algorithm"]["digest_evidence"]
            if evidence["identity"]["name"] == "app-02"
        ]
        self.assertEqual(len(changed), 1)
        evidence = changed[0]
        self.assertFalse(evidence["root_equal"])
        self.assertGreater(evidence["sample_only_diff_count"], 0)
        self.assertEqual(evidence["final_recall_estimate"], 1.0)
        self.assertEqual(evidence["false_negative_upper_bound"], 0.0)

    def test_field_threshold_alone_switches_mode(self) -> None:
        desired = {
            "Deployment/ns/app": Resource(
                "Deployment",
                "ns",
                "app",
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "app",
                    "replicas": 1,
                    "image": "img:1",
                    "labels": {
                        f"label-{index}": f"value-{index}"
                        for index in range(8)
                    },
                    "selector": {"matchLabels": {"app": "app"}},
                },
            )
        }
        actual = copy.deepcopy(desired)
        actual["Deployment/ns/app"].body["labels"]["label-3"] = "changed"
        config = EngineConfig.from_dict(
            {
                "max_concurrency": 2,
                "operation_timeout_ms": 100,
                "exact_object_threshold": 100,
                "exact_field_threshold": 5,
                "sampling_ratio": 0.5,
                "tolerance": 0,
                "logical_step_ms": 10,
            }
        )
        result = compute_diff(desired, actual, [], config)
        self.assertEqual(result["algorithm"]["mode"], "digest_sampled")
        self.assertEqual(
            result["algorithm"]["triggers"][0]["condition"],
            "object_field_count",
        )
        self.assertTrue(result["equivalence"]["equivalent"])

    def test_tolerance_ignores_small_non_replica_numeric_change(self) -> None:
        desired = {
            "ConfigMap/ns/config": Resource(
                "ConfigMap",
                "ns",
                "config",
                {
                    "kind": "ConfigMap",
                    "namespace": "ns",
                    "name": "config",
                    "data": {"weight": "100"},
                    "weight": 100,
                },
            )
        }
        actual = copy.deepcopy(desired)
        actual["ConfigMap/ns/config"].body["weight"] = 100.4
        config = EngineConfig.from_dict(
            {
                "max_concurrency": 2,
                "operation_timeout_ms": 100,
                "exact_object_threshold": 100,
                "exact_field_threshold": 100,
                "sampling_ratio": 0.5,
                "tolerance": 0.5,
                "logical_step_ms": 10,
            }
        )
        result = compute_diff(desired, actual, [], config)
        self.assertEqual(result["drifts"], [])


if __name__ == "__main__":
    unittest.main()

