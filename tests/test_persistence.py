from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from image_diag.errors import BusinessError
from image_diag.state import (
    CheckpointStore,
    SnapshotStore,
    StageGuard,
    StateDatabase,
)


class PersistenceTests(unittest.TestCase):
    def test_snapshot_versions_full_replacement_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-snapshot-") as temporary:
            root = Path(temporary)
            database = StateDatabase(root / "state.db")
            snapshots = SnapshotStore(database)
            first = snapshots.commit("mask", {"pixels": "aaaa", "version": 1})
            second = snapshots.commit(
                "mask",
                {"pixels": "bbbb", "version": 2},
                expected_version=first["version"],
                expected_checksum=first["checksum"],
            )
            self.assertEqual(second["version"], 2)
            self.assertEqual(second["replaced_version"], 1)
            self.assertEqual(snapshots.current("mask")["payload"]["pixels"], "bbbb")
            with self.assertRaises(BusinessError) as caught:
                snapshots.commit(
                    "mask",
                    {"pixels": "aaaa", "version": 1},
                    expected_version=first["version"],
                )
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
            current = snapshots.current("mask")
            self.assertEqual(current["version"], 2)
            self.assertEqual(current["payload"]["pixels"], "bbbb")

    def test_snapshot_checksum_conflict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-checksum-") as temporary:
            database = StateDatabase(Path(temporary) / "state.db")
            snapshots = SnapshotStore(database)
            current = snapshots.commit("style", {"strength": 0.7})
            with self.assertRaises(BusinessError) as caught:
                snapshots.commit(
                    "style",
                    {"strength": 0.8},
                    expected_version=current["version"],
                    expected_checksum="0" * 64,
                )
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_CHECKSUM")

    def test_checkpoint_and_stage_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-checkpoint-") as temporary:
            root = Path(temporary)
            database = StateDatabase(root / "state.db")
            checkpoints = CheckpointStore(database)
            saved = checkpoints.save(
                "run-a",
                stage="completion",
                region_id="R1",
                cache_key="key-a",
                content_hash="digest-a",
            )
            self.assertTrue(saved["created"])
            again = checkpoints.save(
                "run-a",
                stage="completion",
                region_id="R1",
                cache_key="key-a",
                content_hash="digest-a",
            )
            self.assertFalse(again["created"])
            self.assertEqual(len(checkpoints.load("run-a")), 1)
            with StageGuard(root / "locks", "stage"):
                with self.assertRaises(BusinessError) as caught:
                    with StageGuard(root / "locks", "stage"):
                        pass
            self.assertEqual(caught.exception.code, "E_STAGE_BUSY")


if __name__ == "__main__":
    unittest.main()
