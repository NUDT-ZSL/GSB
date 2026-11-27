from __future__ import annotations

import unittest

from flight_compile_cache.generator import generate_edge_project, generate_project
from flight_compile_cache.graph import project_fingerprint
from flight_compile_cache.util import canonical_json
from flight_compile_cache.validation import validate_project


class GeneratorTests(unittest.TestCase):
    def test_same_seed_is_byte_deterministic(self) -> None:
        first = generate_project(20261128)
        second = generate_project(20261128)
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_different_seed_changes_project(self) -> None:
        first = generate_project(20261128)
        second = generate_project(20261129)
        self.assertNotEqual(project_fingerprint(first), project_fingerprint(second))

    def test_main_request_oracle_matches_validation(self) -> None:
        project = generate_project()
        validation = validate_project(project)
        self.assertTrue(validation["executable"])
        expected = project["expectations"]["request_expectations"]
        for request_id, item in expected.items():
            observed = validation["requests"][request_id]
            self.assertEqual(item["valid"], observed["ok"], request_id)
            if not item["valid"]:
                self.assertEqual(item["error_code"], observed["error_code"], request_id)

    def test_edge_fixture_reports_graph_business_codes(self) -> None:
        edge = generate_edge_project()
        validation = validate_project(edge)
        codes = {
            model_id: {error["code"] for error in item["errors"]}
            for model_id, item in validation["models"].items()
            if not item["ok"]
        }
        self.assertIn("E_GRAPH_REFERENCE_MISSING", codes["edge_broken_ref"])
        self.assertIn("E_INVALID_DIMENSION", codes["edge_nonpositive_dim"])
        self.assertIn("E_OP_SHAPE_NOT_CLOSED", codes["edge_not_closed"])

    def test_generator_contains_expected_workload_features(self) -> None:
        project = generate_project()
        self.assertEqual(project["cache_config"]["capacity"], 4)
        self.assertGreaterEqual(len(project["events"]), 2)
        self.assertGreater(project["expectations"]["invalid_request_count"], 0)
        self.assertGreater(project["expectations"]["distinct_compile_key_count"], 0)
        self.assertGreater(len(project["expectations"]["shape_segment_distribution"]), 0)


if __name__ == "__main__":
    unittest.main()
