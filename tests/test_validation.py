from __future__ import annotations

import unittest

from kdrift.validation import load_bundle, parse_selector, selector_matches


class ValidationTests(unittest.TestCase):
    def test_bad_resource_and_duplicate_are_isolated(self) -> None:
        expected = {
            "objects": [
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "valid",
                    "replicas": 1,
                    "image": "img:1",
                    "labels": {"app": "valid"},
                    "selector": {"matchLabels": {"app": "valid"}},
                },
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "bad",
                    "replicas": "2",
                    "image": "img:1",
                    "labels": {"app": "bad"},
                    "selector": {"matchLabels": {"app": "bad"}},
                },
                {
                    "kind": "ConfigMap",
                    "namespace": "ns",
                    "name": "same",
                    "data": {"k": "v"},
                },
                {
                    "kind": "Service",
                    "namespace": "ns",
                    "name": "same",
                    "selector": {"matchLabels": {"app": "same"}},
                    "ports": [{"port": 80}],
                },
            ]
        }
        actual = {
            "objects": [
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "valid",
                    "replicas": 1,
                    "image": "img:1",
                    "labels": {"app": "valid"},
                    "selector": {"matchLabels": {"app": "valid"}},
                }
            ]
        }
        config = {
            "max_concurrency": 2,
            "operation_timeout_ms": 100,
            "exact_object_threshold": 10,
            "exact_field_threshold": 10,
            "sampling_ratio": 0.5,
            "tolerance": 0,
            "logical_step_ms": 10,
        }
        desired, actual_resources, _, issues = load_bundle(
            expected, actual, config
        )
        self.assertEqual(set(desired), {"Deployment/ns/valid"})
        self.assertEqual(set(actual_resources), {"Deployment/ns/valid"})
        codes = [issue.code for issue in issues]
        self.assertIn("E_BAD_RESOURCE", codes)
        self.assertEqual(codes.count("E_DUP_KEY"), 2)

    def test_actual_dangling_configmap_reference_is_quarantined(self) -> None:
        expected = {"objects": []}
        actual = {
            "objects": [
                {
                    "kind": "Deployment",
                    "namespace": "ns",
                    "name": "dangling",
                    "replicas": 1,
                    "image": "img:1",
                    "labels": {"app": "dangling"},
                    "selector": {"matchLabels": {"app": "dangling"}},
                    "configMapRefs": ["missing"],
                },
                {
                    "kind": "ConfigMap",
                    "namespace": "ns",
                    "name": "ok",
                    "data": {"k": "v"},
                },
            ]
        }
        config = {
            "max_concurrency": 2,
            "operation_timeout_ms": 100,
            "exact_object_threshold": 10,
            "exact_field_threshold": 10,
            "sampling_ratio": 0.5,
            "tolerance": 0,
            "logical_step_ms": 10,
        }
        _, actual_resources, _, issues = load_bundle(
            expected, actual, config
        )
        self.assertEqual(set(actual_resources), {"ConfigMap/ns/ok"})
        self.assertEqual([issue.code for issue in issues], ["E_DANGLING_REF"])

    def test_selector_string_and_matching(self) -> None:
        selector = parse_selector(
            "app in (api,worker),tier=backend,!disabled"
        )
        self.assertTrue(
            selector_matches(
                selector,
                {"app": "api", "tier": "backend"},
            )
        )
        self.assertFalse(
            selector_matches(
                selector,
                {"app": "api", "tier": "backend", "disabled": "true"},
            )
        )


if __name__ == "__main__":
    unittest.main()

