from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.helpers import copy_sample, run_cli


class ValidationTests(unittest.TestCase):
    def _mutate_json(self, data_dir: Path, relative: str, mutator) -> None:
        path = data_dir / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload = mutator(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _assert_load_fails_without_state(self, data_dir: Path, expected_code: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "state.db"
            completed, payload = run_cli(
                ["load", "--data-dir", str(data_dir), "--db", str(db)]
            )
            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], expected_code)
            self.assertFalse(db.exists())

    def test_duplicate_application_sequence_is_rejected_before_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))

            def mutate(payload):
                duplicate = dict(payload[0])
                duplicate["application_seq"] = payload[1]["application_seq"]
                return payload + [duplicate]

            self._mutate_json(data_dir, "applications.json", mutate)
            self._assert_load_fails_without_state(
                data_dir, "E_DUPLICATE_APPLICATION_SEQ"
            )

    def test_unknown_resource_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))

            def mutate(payload):
                payload[0]["resource_key"] = "other-resource"
                return payload

            self._mutate_json(data_dir, "applications.json", mutate)
            self._assert_load_fails_without_state(
                data_dir, "E_RESOURCE_KEY_NOT_CLOSED"
            )

    def test_nonfinite_elasticity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))

            def mutate(payload):
                payload[0]["elasticity_ratio"] = float("inf")
                return payload

            self._mutate_json(data_dir, "quota_configs.json", mutate)
            self._assert_load_fails_without_state(data_dir, "E_NONFINITE_NUMBER")

    def test_v1_relative_seconds_must_resolve_to_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))

            def mutate(payload):
                payload["effective_after_seconds"] = "0.0001"
                return payload

            self._mutate_json(data_dir, "snapshots/v1/policy.json", mutate)
            self._assert_load_fails_without_state(
                data_dir, "E_V1_TIME_PRECISION"
            )

    def test_v2_sqlite_unknown_threshold_tenant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))
            metadata = data_dir / "snapshots" / "v2" / "main" / "meta.sqlite"
            connection = sqlite3.connect(metadata)
            try:
                connection.execute(
                    "UPDATE thresholds SET tenant='t-unknown' WHERE tenant='t-alpha'"
                )
                connection.commit()
            finally:
                connection.close()
            self._assert_load_fails_without_state(data_dir, "E_UNKNOWN_TENANT")

    def test_snapshot_generation_must_be_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = copy_sample(Path(temporary))

            def mutate_v1(payload):
                payload["generation"] = 2
                return payload

            def mutate_v2(payload):
                payload["generation"] = 2
                return payload

            self._mutate_json(data_dir, "snapshots/v1/policy.json", mutate_v1)
            self._mutate_json(data_dir, "snapshots/v2/main/policy.json", mutate_v2)
            with tempfile.TemporaryDirectory() as temporary_db:
                db = Path(temporary_db) / "state.db"
                completed, payload = run_cli(
                    ["load", "--data-dir", str(data_dir), "--db", str(db)]
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn(
                    payload["error"]["code"],
                    {
                        "E_SNAPSHOT_GENERATION_ORDER",
                        "E_TOMBSTONE_GENERATION",
                    },
                )
                self.assertFalse(db.exists())

