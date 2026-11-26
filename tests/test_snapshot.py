from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dialog_state_engine.errors import (
    E_FORMAT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    EngineError,
)
from dialog_state_engine.snapshot import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="dialog-snapshot-tests-")
        self.database = Path(self._temp.name) / "snapshots.sqlite"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_versioned_whole_snapshot_replacement(self) -> None:
        first_store = SnapshotStore(self.database)
        first = first_store.commit(
            version=1,
            payload={"kept_old_field": True, "value": 1},
            committed_at_ms=1000,
        )
        loaded = first_store.load_with_status()
        self.assertIsNotNone(loaded.snapshot)
        self.assertFalse(loaded.replaced_cached_snapshot)

        second_store = SnapshotStore(self.database)
        second = second_store.commit(
            version=2,
            payload={"value": 2},
            committed_at_ms=2000,
        )
        self.assertNotEqual(first.checksum, second.checksum)
        changed = first_store.load_with_status()
        self.assertTrue(changed.replaced_cached_snapshot)
        self.assertEqual(changed.snapshot.version, 2)
        self.assertEqual(changed.snapshot.payload, {"value": 2})
        self.assertNotIn("kept_old_field", changed.snapshot.payload)
        stable = first_store.load_with_status()
        self.assertFalse(stable.replaced_cached_snapshot)

    def test_rollback_and_same_version_different_checksum_are_rejected(self) -> None:
        store = SnapshotStore(self.database)
        store.commit(
            version=2,
            payload={"value": 2},
            committed_at_ms=2000,
        )
        with self.assertRaises(EngineError) as lower:
            store.commit(
                version=1,
                payload={"value": 1},
                committed_at_ms=3000,
            )
        self.assertEqual(lower.exception.code, E_SNAPSHOT_ROLLBACK)
        with self.assertRaises(EngineError) as same:
            store.commit(
                version=2,
                payload={"value": 99},
                committed_at_ms=4000,
            )
        self.assertEqual(same.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(store.load().payload, {"value": 2})

    def test_commit_is_immediately_stage_busy(self) -> None:
        store = SnapshotStore(self.database)
        store.commit(
            version=1,
            payload={"value": 1},
            committed_at_ms=1000,
        )
        with store.hold_stage():
            with self.assertRaises(EngineError) as caught:
                store.commit(
                    version=2,
                    payload={"value": 2},
                    committed_at_ms=2000,
                )
        self.assertEqual(caught.exception.code, E_STAGE_BUSY)
        self.assertEqual(store.load().payload, {"value": 1})

    def test_checksum_tampering_is_detected(self) -> None:
        store = SnapshotStore(self.database)
        store.commit(
            version=1,
            payload={"value": 1},
            committed_at_ms=1000,
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE snapshot SET payload_json = ? WHERE singleton = 1",
                (json.dumps({"value": 2}),),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EngineError) as caught:
            store.load()
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(caught.exception.field, "snapshot.checksum")

    def _run_child(self, code: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_forced_termination_keeps_last_committed_snapshot(self) -> None:
        store = SnapshotStore(self.database)
        store.commit(
            version=1,
            payload={"value": 1},
            committed_at_ms=1000,
        )
        database = json.dumps(str(self.database))
        code = f"""
from dialog_state_engine.snapshot import SnapshotStore
import os
store = SnapshotStore({database})
with store.hold_stage():
    os._exit(19)
"""
        process = self._run_child(code)
        self.assertEqual(process.returncode, 19, process.stderr)
        self.assertEqual(store.load().payload, {"value": 1})

    def test_committed_state_survives_immediate_forced_exit(self) -> None:
        store = SnapshotStore(self.database)
        store.commit(
            version=1,
            payload={"value": 1},
            committed_at_ms=1000,
        )
        database = json.dumps(str(self.database))
        code = f"""
from dialog_state_engine.snapshot import SnapshotStore
import os
store = SnapshotStore({database})
store.commit(version=2, payload={{"value": 2}}, committed_at_ms=2000)
os._exit(23)
"""
        process = self._run_child(code)
        self.assertEqual(process.returncode, 23, process.stderr)
        recovered = SnapshotStore(self.database).load()
        self.assertEqual(recovered.version, 2)
        self.assertEqual(recovered.payload, {"value": 2})


if __name__ == "__main__":
    unittest.main()

