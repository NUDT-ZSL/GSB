from __future__ import annotations

import unittest

from kdrift.compare import compute_diff
from kdrift.model import Resource
from kdrift.planning import generate_plan
from tests.helpers import SAMPLES, sample_bundle


class PlanningTests(unittest.TestCase):
    def test_small_plan_has_topological_reference_constraints(self) -> None:
        desired, actual, config, _, issues = sample_bundle(SAMPLES)
        diff = compute_diff(desired, actual, issues, config)
        plan = generate_plan(desired, actual, diff["drifts"], config)
        self.assertTrue(plan["valid"])
        order = plan["topological_order"]
        by_id = {
            operation["operation_id"]: operation
            for operation in plan["operations"]
        }
        by_key = {
            operation["resource_key"]: operation
            for operation in plan["operations"]
        }

        cache_cm = by_key["ConfigMap/shop/cache-config"]["operation_id"]
        cache_deploy = by_key["Deployment/shop/cache"]["operation_id"]
        cache_svc = by_key["Service/shop/cache-svc"]["operation_id"]
        self.assertLess(order.index(cache_cm), order.index(cache_deploy))
        self.assertLess(order.index(cache_deploy), order.index(cache_svc))

        legacy_svc = by_key["Service/shop/legacy-svc"]["operation_id"]
        legacy_deploy = by_key["Deployment/shop/legacy"]["operation_id"]
        legacy_cm = by_key["ConfigMap/shop/legacy-config"]["operation_id"]
        self.assertLess(order.index(legacy_svc), order.index(legacy_deploy))
        self.assertLess(order.index(legacy_deploy), order.index(legacy_cm))
        self.assertIn(
            legacy_deploy,
            by_id[legacy_cm]["depends_on"],
        )
        self.assertTrue(
            all(check["safe"] for check in plan["reference_checks"])
        )
        self.assertTrue(
            plan["constraints"]["mutual_exclusion_pairs"]
        )

    def test_unsafe_delete_is_rejected(self) -> None:
        desired = {
            "Service/ns/front": Resource(
                "Service",
                "ns",
                "front",
                {
                    "kind": "Service",
                    "namespace": "ns",
                    "name": "front",
                    "labels": {"app": "front"},
                    "selector": {"matchLabels": {"app": "orphan"}},
                    "ports": [{"port": 80, "targetPort": 8080}],
                },
            )
        }
        actual = {
            "Service/ns/front": desired["Service/ns/front"],
            "Deployment/ns/orphan": Resource(
                "Deployment",
                "ns",
                "orphan",
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "orphan",
                    "replicas": 1,
                    "image": "img:1",
                    "labels": {"app": "orphan"},
                    "selector": {"matchLabels": {"app": "orphan"}},
                },
            ),
        }
        config = sample_bundle(SAMPLES)[2]
        diff = compute_diff(desired, actual, [], config)
        plan = generate_plan(desired, actual, diff["drifts"], config)
        self.assertFalse(plan["valid"])
        self.assertEqual(
            plan["violations"][0]["code"], "E_UNSAFE_DELETE"
        )


if __name__ == "__main__":
    unittest.main()

