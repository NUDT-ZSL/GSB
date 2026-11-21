from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capacity_engine.errors import BizError
from capacity_engine.scene import load_scene, parse_scene

from tests.helpers import SAMPLE_SCENE, run_cli, write_json


class SceneValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(SAMPLE_SCENE.read_text(encoding="utf-8"))

    def assert_code(self, document: dict, code: str) -> None:
        with self.assertRaises(BizError) as caught:
            parse_scene(document)
        self.assertEqual(caught.exception.code, code)

    def test_valid_sample(self) -> None:
        scene = load_scene(SAMPLE_SCENE)
        self.assertEqual(scene.scene_id, "deterministic-capacity-v1")
        self.assertEqual(len(scene.batches), 2)
        self.assertEqual(scene.tenants["tenant-a"].concurrency_limit, 16)
        self.assertEqual(scene.precision.percent_scale, 10000)

    def test_duplicate_service(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"].append(copy.deepcopy(document["services"][0]))
        self.assert_code(document, "E_DUPLICATE_SERVICE")

    def test_duplicate_tenant(self) -> None:
        document = copy.deepcopy(self.document)
        duplicate_service = copy.deepcopy(document["services"][0])
        duplicate_service["name"] = "checkout-duplicate"
        document["services"].append(duplicate_service)
        self.assert_code(document, "E_DUPLICATE_TENANT")

    def test_non_loopback_address(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"][0]["address"] = "http://192.0.2.1:18080"
        self.assert_code(document, "E_INVALID_ADDRESS")

    def test_gradient_must_strictly_increase(self) -> None:
        document = copy.deepcopy(self.document)
        document["batches"][0]["concurrency_gradients"] = [1, 2, 2]
        self.assert_code(document, "E_INVALID_GRADIENT")

    def test_positive_duration_and_pace(self) -> None:
        document = copy.deepcopy(self.document)
        document["batches"][0]["duration_logical_ms"] = 0
        self.assert_code(document, "E_INVALID_DURATION")
        document = copy.deepcopy(self.document)
        document["batches"][0]["request_pace_ms"] = 0
        self.assert_code(document, "E_INVALID_PACE")

    def test_slo_ranges(self) -> None:
        document = copy.deepcopy(self.document)
        document["services"][0]["tenants"]["tenant-a"]["success_rate_min"] = 1.2
        self.assert_code(document, "E_INVALID_SLO")
        document = copy.deepcopy(self.document)
        document["services"][0]["tenants"]["tenant-a"]["p95_latency_max_ms"] = 0
        self.assert_code(document, "E_INVALID_SLO")

    def test_precision_scales_and_tolerances(self) -> None:
        document = copy.deepcopy(self.document)
        document["precision"]["latency_scale"] = 0
        self.assert_code(document, "E_INVALID_SCALE")
        document = copy.deepcopy(self.document)
        document["precision"]["compare_tolerance"]["success_rate"] = 0
        self.assert_code(document, "E_INVALID_TOLERANCE")
        document = copy.deepcopy(self.document)
        document["precision"]["error_budget"]["p95_latency_ms"] = -1
        self.assert_code(document, "E_INVALID_BUDGET")

    def test_invalid_scene_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            invalid = copy.deepcopy(self.document)
            invalid["batches"][0]["concurrency_gradients"] = [1, 1]
            scene_path = write_json(base / "invalid-scene.json", invalid)
            db_path = base / "state.sqlite"
            result = run_cli("run", "--scene", str(scene_path), "--db", str(db_path))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)["error"]["code"], "E_INVALID_GRADIENT")
            self.assertFalse(db_path.exists())
            self.assertFalse(db_path.with_suffix(".sqlite.lock").exists())


if __name__ == "__main__":
    unittest.main()

