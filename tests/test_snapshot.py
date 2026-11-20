from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from blockops.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_SNAPSHOT_UNKNOWN,
)
from blockops.snapshot import Snapshot, SnapshotStore
from blockops.util import atomic_write_json


class SnapshotTests(unittest.TestCase):
    def test_whole_object_replacement(self) -> None:
        store = SnapshotStore()
        v1 = store.publish("plan", {"a": 1, "b": 2})
        self.assertTrue(v1.ok)
        v2 = store.publish("plan", {"a": 3})
        self.assertTrue(v2.ok)
        query = store.query("plan")
        self.assertTrue(query["ok"])
        self.assertEqual(query["version"], 2)
        self.assertEqual(query["content"], {"a": 3})
        self.assertNotIn("b", query["content"])
        self.assertNotEqual(v1.snapshot.checksum, v2.snapshot.checksum)

    def test_rollback_is_rejected_and_current_snapshot_survives(self) -> None:
        store = SnapshotStore()
        v1 = store.publish("plan", {"value": 1})
        store.publish("plan", {"value": 2})
        rollback = store.commit(v1.snapshot)
        self.assertFalse(rollback.ok)
        self.assertEqual(rollback.code, E_SNAPSHOT_ROLLBACK)
        self.assertIn("snapshot_management:plan", rollback.error["stage"])
        self.assertEqual(rollback.error["details"]["attempted_version"], 1)
        self.assertEqual(rollback.error["details"]["current_version"], 2)
        self.assertEqual(store.query("plan")["content"], {"value": 2})

    def test_checksum_mismatch_is_corrupt_with_stage_and_version(self) -> None:
        store = SnapshotStore()
        operation = Snapshot.create(
            "tolerance", 7, {"abs_tol": 1e-12}, checksum="0" * 64
        )
        result = store.commit(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(result.error["stage"], "snapshot_management:tolerance")
        self.assertEqual(result.error["category"], "tolerance")
        self.assertEqual(result.error["version"], 7)
        self.assertEqual(result.error["details"]["version"], 7)

    def test_same_version_different_checksum_is_corrupt(self) -> None:
        store = SnapshotStore()
        store.publish("tolerance", {"abs_tol": 1e-12}, version=1)
        conflicting = Snapshot.create("tolerance", 1, {"abs_tol": 1e-9})
        result = store.commit(conflicting)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(store.query("tolerance")["content"], {"abs_tol": 1e-12})

    def test_persistence_roundtrip_and_file_corruption_detection(self) -> None:
        store = SnapshotStore()
        store.publish("block_plan", {"blocks": [[0, 4], [4, 8]]})
        store.publish("job_statistics:job-1", {"mean": [0.1, 0.2]})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshots.json"
            store.write(path)
            loaded, error = SnapshotStore.load(path)
            self.assertIsNone(error)
            self.assertEqual(
                loaded.query("block_plan")["content"],
                {"blocks": [[0, 4], [4, 8]]},
            )
            self.assertEqual(
                loaded.query("job_statistics:job-1")["version"], 1
            )

            document = json.loads(path.read_text(encoding="utf-8"))
            document["snapshots"]["block_plan"]["content"]["blocks"] = [[0, 1]]
            atomic_write_json(path, document)
            corrupted, error = SnapshotStore.load(path)
            self.assertIsNotNone(error)
            self.assertEqual(error["code"], E_SNAPSHOT_CORRUPT)
            self.assertIn("block_plan", error["stage"])
            self.assertEqual(error["details"]["version"], 1)

    def test_unknown_category_is_reported(self) -> None:
        store = SnapshotStore()
        result = store.query("missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], E_SNAPSHOT_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
