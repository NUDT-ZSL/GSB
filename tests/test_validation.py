from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from topodiag.errors import BusinessError
from topodiag.policy import load_v1_policy, load_v2_package
from topodiag.sample import generate_samples
from topodiag.util import read_json, write_json_atomic
from topodiag.validation import (
    validate_requests,
    validate_template_manifest,
)

from .helpers import make_workdir


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = make_workdir()
        generate_samples(self.workdir)
        self.templates_payload = read_json(
            self.workdir / "samples" / "templates.json"
        )
        self.requests_payload = read_json(
            self.workdir / "samples" / "requests.json"
        )
        _, _, self.templates = validate_template_manifest(self.templates_payload)

    def test_missing_dependency_has_standard_diagnostics(self) -> None:
        payload = json.loads(json.dumps(self.templates_payload))
        payload["templates"][0]["dependencies"] = ["does-not-exist"]
        with self.assertRaises(BusinessError) as caught:
            validate_template_manifest(payload)
        error = caught.exception
        self.assertEqual(error.code, "MISSING_DEPENDENCY")
        self.assertEqual(
            set(error.diagnostics),
            {
                "template_name",
                "version",
                "shard_index",
                "request_seq",
                "missing_dependency",
            },
        )
        self.assertEqual(error.diagnostics["template_name"], "base")
        self.assertEqual(error.diagnostics["missing_dependency"], "does-not-exist")

    def test_dependency_cycle_is_rejected(self) -> None:
        payload = json.loads(json.dumps(self.templates_payload))
        by_name = {item["name"]: item for item in payload["templates"]}
        by_name["base"]["dependencies"] = ["catalog"]
        by_name["catalog"]["dependencies"] = ["base"]
        with self.assertRaises(BusinessError) as caught:
            validate_template_manifest(payload)
        self.assertEqual(caught.exception.code, "DEPENDENCY_CYCLE")

    def test_duplicate_template_name_is_rejected(self) -> None:
        payload = json.loads(json.dumps(self.templates_payload))
        payload["templates"].append(json.loads(json.dumps(payload["templates"][0])))
        with self.assertRaises(BusinessError) as caught:
            validate_template_manifest(payload)
        self.assertEqual(caught.exception.code, "TEMPLATE_NAME_DUPLICATE")

    def test_resource_requirement_must_be_positive_integer(self) -> None:
        payload = json.loads(json.dumps(self.templates_payload))
        payload["templates"][0]["resource_requirements"]["cpu_millis"] = 0
        with self.assertRaises(BusinessError) as caught:
            validate_template_manifest(payload)
        self.assertEqual(caught.exception.code, "INVALID_RESOURCE_REQUIREMENT")

    def test_invalid_label_is_rejected(self) -> None:
        payload = json.loads(json.dumps(self.templates_payload))
        payload["templates"][0]["labels"] = ["bad label"]
        with self.assertRaises(BusinessError) as caught:
            validate_template_manifest(payload)
        self.assertEqual(caught.exception.code, "INVALID_LABEL")

    def test_duplicate_request_sequence_is_rejected(self) -> None:
        payload = json.loads(json.dumps(self.requests_payload))
        payload["requests"][1]["request_seq"] = payload["requests"][0]["request_seq"]
        with self.assertRaises(BusinessError) as caught:
            validate_requests(payload, templates=self.templates)
        self.assertEqual(caught.exception.code, "REQUEST_SEQ_DUPLICATE")
        self.assertIsNotNone(caught.exception.diagnostics["request_seq"])

    def test_request_clock_rollback_beyond_tolerance_is_rejected(self) -> None:
        payload = json.loads(json.dumps(self.requests_payload))
        payload["requests"][1]["logical_clock"] = 10_000
        with self.assertRaises(BusinessError) as caught:
            validate_requests(
                payload,
                templates=self.templates,
                clock_tolerance_ms=100,
            )
        self.assertEqual(caught.exception.code, "CLOCK_ROLLBACK")
        self.assertEqual(caught.exception.diagnostics["request_seq"], 2)

    def test_v1_relative_seconds_must_be_nonnegative(self) -> None:
        path = self.workdir / "samples" / "policies" / "v1.json"
        payload = read_json(path)
        payload["relative_effective_seconds"] = -1
        write_json_atomic(path, payload)
        with self.assertRaises(BusinessError) as caught:
            load_v1_policy(path)
        self.assertEqual(caught.exception.code, "INVALID_V1_RELATIVE_TIME")

    def test_v2_shard_hash_mismatch_has_shard_diagnostics(self) -> None:
        copied = self.workdir / "tampered-v2"
        shutil.copytree(
            self.workdir / "samples" / "policies" / "v2",
            copied,
        )
        shard = copied / "shard-001.json"
        shard.write_bytes(shard.read_bytes() + b"\n")
        with self.assertRaises(BusinessError) as caught:
            load_v2_package(copied)
        error = caught.exception
        self.assertEqual(error.code, "SHARD_HASH_MISMATCH")
        self.assertEqual(error.diagnostics["shard_index"], 1)
        self.assertEqual(error.diagnostics["version"], "2.0.0")
        self.assertIn("expected_sha256", error.diagnostics)
        self.assertIn("actual_sha256", error.diagnostics)


if __name__ == "__main__":
    unittest.main()
