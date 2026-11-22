from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quantlab.errors import (
    BusinessError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from quantlab.snapshots import SnapshotStore


class SnapshotTests(unittest.TestCase):
    def _store(self, root: Path) -> SnapshotStore:
        return SnapshotStore(root / "state", root / "tmp")

    def test_partial_progress_only_lists_fully_completed_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            snapshot = store.commit(
                stage="quantization",
                completed_layers=["layer_00"],
                stage_complete=False,
                next_layer_index=1,
                manifest_digest="digest",
                payload={"done": 1},
            )
            self.assertEqual(snapshot.version, 1)
            loaded = store.latest()
            self.assertEqual(loaded.completed_layers, ("layer_00",))
            self.assertFalse(loaded.stage_complete)
            self.assertEqual(store.progress_view()["next_version"], 2)

    def test_corrupt_snapshot_reports_stage_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            snapshot = store.commit(
                stage="calibration",
                completed_layers=["layer_00"],
                stage_complete=False,
                next_layer_index=1,
                manifest_digest="digest",
                payload={"value": 42},
            )
            path = root / "state" / snapshot.path
            value = json.loads(path.read_text(encoding="utf-8"))
            value["payload"]["value"] = 99
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(BusinessError) as captured:
                store.latest()
            error = captured.exception
            self.assertEqual(error.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(error.stage, "calibration")
            self.assertEqual(error.version, 1)

    def test_concurrent_commit_is_immediate_stage_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            with store.hold_commit_lock(stage="quantization"):
                with self.assertRaises(BusinessError) as captured:
                    store.commit(
                        stage="quantization",
                        completed_layers=[],
                        stage_complete=False,
                        next_layer_index=0,
                        manifest_digest="digest",
                        payload={},
                    )
            self.assertEqual(captured.exception.code, E_STAGE_BUSY)
            self.assertEqual(captured.exception.stage, "quantization")

    def test_old_version_cannot_overwrite_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            first = store.commit(
                stage="inference",
                completed_layers=["layer_00"],
                stage_complete=False,
                next_layer_index=1,
                manifest_digest="digest",
                payload={"v": 1},
            )
            second = store.commit(
                stage="inference",
                completed_layers=["layer_00", "layer_01"],
                stage_complete=True,
                next_layer_index=2,
                manifest_digest="digest",
                payload={"v": 2},
            )
            self.assertGreater(second.version, first.version)
            with self.assertRaises(BusinessError) as captured:
                store.commit(
                    stage="inference",
                    completed_layers=["layer_00"],
                    stage_complete=False,
                    next_layer_index=1,
                    manifest_digest="digest",
                    payload={"v": 1},
                    requested_version=first.version,
                )
            self.assertEqual(captured.exception.code, E_SNAPSHOT_ROLLBACK)
            self.assertEqual(store.latest().payload["v"], 2)

    def test_dead_process_lock_is_reclaimed_after_hard_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = self._store(root)
            store.lock_path.write_text("pid=999999999\n", encoding="ascii")
            snapshot = store.commit(
                stage="inference",
                completed_layers=[],
                stage_complete=False,
                next_layer_index=0,
                manifest_digest="digest",
                payload={},
            )
            self.assertEqual(snapshot.version, 1)


if __name__ == "__main__":
    unittest.main()
