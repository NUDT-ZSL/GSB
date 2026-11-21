from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    copy_sample,
    read_json,
    rebuild_v2_metadata,
    run_cli,
    write_json,
)


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = copy_sample(self.root)
        self.db = self.root / "state.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_code(self, code: str) -> None:
        exit_code, payload = run_cli(self.db, self.package, "load")
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["code"], code)
        self.assertFalse(payload["ok"])
        self.assertFalse(self.db.exists(), "invalid input must not initialize state")

    def test_missing_package(self) -> None:
        exit_code, payload = run_cli(
            self.db, self.root / "missing-package.json", "load"
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["code"], "CONFIG_PACKAGE_MISSING")
        self.assertFalse(self.db.exists())

    def test_malformed_json(self) -> None:
        (self.package.parent / "v1_manifest.json").write_text("{broken", encoding="utf-8")
        self.assert_code("JSON_MALFORMED")

    def test_duplicate_service_environment(self) -> None:
        path = self.package.parent / "v1_manifest.json"
        document = read_json(path)
        document["records"].append(dict(document["records"][0]))
        write_json(path, document)
        self.assert_code("DUPLICATE_SERVICE_ENV")

    def test_invalid_replicas_and_retry(self) -> None:
        path = self.package.parent / "v1_manifest.json"
        document = read_json(path)
        document["records"][0]["replicas"] = 0
        write_json(path, document)
        self.assert_code("INVALID_REPLICAS")

        document = read_json(self.package.parent / "v1_manifest.json")
        document["records"][0]["replicas"] = 1
        document["records"][0]["retry_factor"] = 0
        write_json(path, document)
        self.assert_code("INVALID_RETRY_FACTOR")

    def test_parameter_declaration_mismatch(self) -> None:
        path = self.package.parent / "v1_manifest.json"
        document = read_json(path)
        document["records"][0]["params"]["replicas"] = 99
        write_json(path, document)
        self.assert_code("PARAM_DECLARATION_MISMATCH")

    def test_invalid_timeout_and_relative_time(self) -> None:
        path = self.package.parent / "v1_manifest.json"
        document = read_json(path)
        document["records"][0]["timeout_ms"] = -1
        write_json(path, document)
        self.assert_code("INVALID_TIMEOUT")

        document = read_json(path)
        document["records"][0]["timeout_ms"] = 500
        document["records"][0]["effective_after_s"] = -1
        write_json(path, document)
        self.assert_code("INVALID_EFFECTIVE_AFTER")

    def test_shard_hash_mismatch(self) -> None:
        path = self.package.parent / "shards" / "initial.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assert_code("SHARD_HASH_MISMATCH")

    def test_generation_regression(self) -> None:
        path = self.package.parent / "shards" / "initial.json"
        document = read_json(path)
        for record in document["records"]:
            if record["service"] == "billing":
                record["generation"] = 3
                break
        write_json(path, document)
        rebuild_v2_metadata(self.package)
        self.assert_code("GENERATION_REGRESSION")

    def test_dependency_cycle(self) -> None:
        path = self.package.parent / "shards" / "initial.json"
        document = read_json(path)
        for record in document["records"]:
            if record["service"] == "auth":
                record["upstreams"] = ["orders"]
                break
        write_json(path, document)
        rebuild_v2_metadata(self.package)
        self.assert_code("DEPENDENCY_CYCLE")

    def test_missing_upstream(self) -> None:
        path = self.package.parent / "shards" / "initial.json"
        document = read_json(path)
        for record in document["records"]:
            if record["service"] == "auth":
                record["upstreams"] = ["ghost"]
                break
        write_json(path, document)
        rebuild_v2_metadata(self.package)
        self.assert_code("MISSING_UPSTREAM")

    def test_absolute_time_must_be_integer(self) -> None:
        path = self.package.parent / "shards" / "initial.json"
        document = read_json(path)
        document["records"][0]["effective_at_ms"] = 12.5
        write_json(path, document)
        rebuild_v2_metadata(self.package)
        self.assert_code("INVALID_EFFECTIVE_AT")


if __name__ == "__main__":
    unittest.main()

