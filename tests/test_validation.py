from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from batchledger.fixture import load_fixture
from tests.support import cli, fixture_bundle, write_bundle
from tools.generate_samples import generate


class ValidationTests(unittest.TestCase):
    def test_valid_fixture_loads(self) -> None:
        fixture = load_fixture(Path(__file__).resolve().parents[1] / "samples" / "default")
        self.assertEqual(fixture.model.model_id, "ledger-demo-1.8b")
        self.assertEqual(len(fixture.requests), 48)
        self.assertTrue(fixture.content_hash)

    def test_invalid_fixtures_return_business_codes_without_state(self) -> None:
        cases = [
            ("duplicate_replica", "E_REPLICA_ID_DUPLICATE"),
            ("replica_compute", "E_REPLICA_COMPUTE_INVALID"),
            ("replica_window", "E_REPLICA_WINDOW_INVALID"),
            ("request_duplicate", "E_REQUEST_ID_DUPLICATE"),
            ("request_arrival", "E_REQUEST_ARRIVAL_INVALID"),
            ("sequence_limit", "E_REQUEST_SEQUENCE_EXCEEDED"),
            ("tolerance", "E_ACCURACY_TOLERANCE_INVALID"),
            ("scale", "E_ACCURACY_SCALE_INVALID"),
        ]
        for name, expected_code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                bundle = fixture_bundle()
                if name == "duplicate_replica":
                    bundle["replicas"].append(dict(bundle["replicas"][0]))
                elif name == "replica_compute":
                    bundle["replicas"][0]["compute_capacity"] = 0
                elif name == "replica_window":
                    bundle["replicas"][0]["window_ms"] = 0
                elif name == "request_duplicate":
                    bundle["requests"][1]["request_id"] = bundle["requests"][0]["request_id"]
                elif name == "request_arrival":
                    bundle["requests"][0]["arrival_ms"] = -1
                elif name == "sequence_limit":
                    for replica in bundle["replicas"]:
                        replica["max_sequence_length"] = 16
                elif name == "tolerance":
                    bundle["accuracy"]["float_tolerance"] = 0
                elif name == "scale":
                    bundle["accuracy"]["memory_scale"] = -2
                fixture_path = write_bundle(temp_path / "fixture.json", bundle)
                db_path = temp_path / "state.db"
                result = cli("load", str(fixture_path), "--db", str(db_path))
                self.assertEqual(result.returncode, 2, result.stderr)
                payload = json.loads(result.stderr)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], expected_code)
                self.assertFalse(db_path.exists())
                self.assertFalse(Path(str(db_path) + ".lock").exists())

    def test_invalid_json_has_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            fixture_path = temp_path / "bad.json"
            fixture_path.write_text("{not json", encoding="utf-8")
            db_path = temp_path / "state.db"
            result = cli("load", str(fixture_path), "--db", str(db_path))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                json.loads(result.stderr)["error"]["code"], "E_JSON_INVALID"
            )
            self.assertFalse(db_path.exists())

    def test_invalid_reset_does_not_delete_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            valid_path = write_bundle(temp_path / "valid.json", fixture_bundle())
            db_path = temp_path / "state.db"
            loaded = cli("load", str(valid_path), "--db", str(db_path))
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            before = db_path.read_bytes()
            invalid = fixture_bundle()
            invalid["accuracy"]["error_budget"] = 0
            invalid_path = write_bundle(temp_path / "invalid.json", invalid)
            failed = cli(
                "load",
                str(invalid_path),
                "--db",
                str(db_path),
                "--reset",
            )
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(db_path.read_bytes(), before)

    def test_fixed_seed_generation_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            generate(Path(first), 20261126)
            generate(Path(second), 20261126)
            first_files = {
                path.name: path.read_bytes()
                for path in sorted(Path(first).iterdir())
            }
            second_files = {
                path.name: path.read_bytes()
                for path in sorted(Path(second).iterdir())
            }
            self.assertEqual(first_files, second_files)

    def test_nested_database_parent_is_created_only_after_valid_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "a" / "b" / "state.db"
            result = cli(
                "load",
                str(Path(__file__).resolve().parents[1] / "samples" / "default"),
                "--db",
                str(db_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
