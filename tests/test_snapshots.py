from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from numeric_workbench.errors import WorkbenchError
from numeric_workbench.snapshots import SNAPSHOT_STAGES, SnapshotStore


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.temp.name)
        self.payloads = {stage: {"generation": 1, "values": [1, 2, 3]} for stage in SNAPSHOT_STAGES}

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, generation: int, versions=None):
        payloads = {
            stage: {"generation": generation, "values": [generation, 2, 3]}
            for stage in SNAPSHOT_STAGES
        }
        return self.store.commit(
            "job",
            stage_payloads=payloads,
            input_sha256="input",
            config_sha256="config",
            versions=versions,
        )

    def test_versions_increase_and_payload_is_whole_replaced(self):
        first = self.commit(1)
        second = self.commit(2)
        self.assertEqual(first["versions"]["chunk_plan"], 1)
        self.assertEqual(second["versions"]["chunk_plan"], 2)
        state = self.store.load("job")
        self.assertEqual(state["replacement_mode"], "whole_snapshot_replace")
        self.assertEqual(
            state["stages"]["precision_error_spectra"]["payload"]["generation"], 2
        )
        self.assertTrue(second["replaced_stages"])

    def test_old_version_over_new_returns_rollback(self):
        self.commit(1)
        self.commit(2)
        with self.assertRaises(WorkbenchError) as context:
            self.commit(0, versions={stage: 1 for stage in SNAPSHOT_STAGES})
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(context.exception.details["stage"], "normalization_statistics")
        self.assertEqual(context.exception.details["current_version"], 2)

    def test_checksum_mismatch_returns_corrupt_with_stage_and_version(self):
        self.commit(1)
        path = self.store.path("job")
        state = json.loads(path.read_text(encoding="utf-8"))
        state["stages"]["chunk_plan"]["payload"]["generation"] = 999
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(WorkbenchError) as context:
            self.store.load("job")
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(context.exception.details["stage"], "chunk_plan")
        self.assertEqual(context.exception.details["version"], 1)

    def test_same_version_checksum_change_replaces_stage(self):
        self.commit(1)
        summary = self.commit(5, versions={stage: 1 for stage in SNAPSHOT_STAGES})
        reasons = {item["stage"]: item["reason"] for item in summary["replaced_stages"]}
        self.assertEqual(reasons["chunk_plan"], "checksum_change_same_version")
        state = self.store.load("job")
        self.assertEqual(state["stages"]["chunk_plan"]["payload"]["generation"], 5)


if __name__ == "__main__":
    unittest.main()
