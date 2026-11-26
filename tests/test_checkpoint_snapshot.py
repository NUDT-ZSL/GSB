from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from index_diag.checkpoint import Checkpoint, CheckpointStore
from index_diag.errors import (
    DiagnosticError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STAGE_INCOMPLETE,
)
from index_diag.ledger import LedgerManager, VersionedSnapshotStore
from index_diag.locking import NonBlockingFileLock


class CheckpointSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, function, *args, **kwargs) -> None:
        with self.assertRaises(DiagnosticError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_checkpoint_versions_phases_and_checksums(self) -> None:
        store = CheckpointStore(self.root / "checkpoint.json")
        self.assert_error(
            E_STAGE_INCOMPLETE,
            store.commit,
            phase="analyze",
            completed_by_phase={"analyze": []},
            expected_batches=[1],
            payload={"documents": []},
        )
        first = store.commit(
            phase="analyze",
            completed_by_phase={"analyze": [1]},
            expected_batches=[1],
            payload={"documents": [1]},
        )
        self.assertEqual(first.version, 1)
        second = store.commit(
            phase="postings",
            completed_by_phase={"analyze": [1], "postings": [1]},
            expected_batches=[1],
            payload={"documents": [1], "postings": {"a": [1]}},
        )
        self.assertEqual(second.version, 2)
        loaded = store.load()
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.completed_by_phase["postings"], [1])

        raw = json.loads((self.root / "checkpoint.json").read_text())
        raw["payload"]["postings"]["a"] = [999]
        (self.root / "checkpoint.json").write_text(json.dumps(raw))
        self.assert_error(E_SNAPSHOT_CORRUPT, store.load)

    def test_checkpoint_explicit_rollback_is_rejected(self) -> None:
        store = CheckpointStore(self.root / "checkpoint.json")
        first = store.commit(
            phase="analyze",
            completed_by_phase={"analyze": [1]},
            expected_batches=[1],
            payload={},
        )
        rolled = Checkpoint.create(
            version=first.version - 1,
            phase="analyze",
            completed_by_phase={"analyze": [1]},
            expected_batches=[1],
            payload={"rolled": True},
        )
        self.assert_error(
            E_SNAPSHOT_ROLLBACK, store.replace_with_explicit_version, rolled
        )

    def test_snapshot_whole_replacement_rollback_and_lock(self) -> None:
        store = VersionedSnapshotStore(
            self.root / "snapshot.json", format_name="test-snapshot"
        )
        store.commit(1, {"old": 1, "value": "first"})
        store.commit(2, {"value": "second"})
        loaded = store.load()
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.body, {"value": "second"})
        self.assertNotIn("old", loaded.body)
        self.assert_error(E_SNAPSHOT_ROLLBACK, store.commit, 1, {"value": "rollback"})
        self.assert_error(E_STAGE_BUSY, store.commit, 2, {"value": "changed"})

        with NonBlockingFileLock(store.lock_path):
            self.assert_error(E_STAGE_BUSY, store.commit, 3, {"value": "busy"})

        raw = json.loads((self.root / "snapshot.json").read_text())
        raw["body"]["value"] = "tampered"
        (self.root / "snapshot.json").write_text(json.dumps(raw))
        self.assert_error(E_SNAPSHOT_CORRUPT, store.load)

        raw["body"]["value"] = "second"
        raw["version"] = 1
        (self.root / "snapshot.json").write_text(json.dumps(raw))
        self.assert_error(E_SNAPSHOT_CORRUPT, store.load)

    def test_ledger_bundle_and_nested_checksums(self) -> None:
        manager = LedgerManager(
            self.root / "ledger.json", self.root / "config_snapshot.json"
        )
        committed = manager.commit(
            1,
            entries=[{"batch_no": 1}],
            statistics={"config_hash": "cfg"},
            config_snapshot={"config_hash": "cfg", "generation": 1, "version": 1},
            batch_digests={"1": "digest"},
        )
        self.assertEqual(committed["bundle_version"], 1)
        loaded = manager.load()
        self.assertEqual(loaded["ledger"]["entries"], [{"batch_no": 1}])
        self.assertEqual(loaded["config_snapshot"]["config_hash"], "cfg")
        self.assertTrue((self.root / "config_snapshot.json").exists())

        raw = json.loads((self.root / "ledger.json").read_text())
        raw["body"]["ledger"]["entries"][0]["batch_no"] = 2
        (self.root / "ledger.json").write_text(json.dumps(raw))
        self.assert_error(E_SNAPSHOT_CORRUPT, manager.load)

    def test_cross_process_commit_lock_returns_stage_busy(self) -> None:
        store = VersionedSnapshotStore(
            self.root / "locked-snapshot.json", format_name="locked"
        )
        store.commit(1, {"version": 1})
        ready = self.root / "ready"
        script = (
            "import fcntl, os, sys, time\n"
            f"fd = os.open({str(store.lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            f"open({str(ready)!r}, 'w').write('ready')\n"
            "time.sleep(5)\n"
        )
        process = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            self.assert_error(E_STAGE_BUSY, store.commit, 2, {"version": 2})
        finally:
            process.terminate()
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
