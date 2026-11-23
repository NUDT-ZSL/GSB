from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path

from sparse_recall.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    RecallError,
)
from sparse_recall.snapshots import VersionedSnapshotStore, nonblocking_commit_lock


ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sparse-recall-snapshot-")
        self.path = Path(self.temporary.name) / "checkpoints" / "checkpoint.json"
        self.store = VersionedSnapshotStore(
            self.path,
            snapshot_kind="pipeline-checkpoint",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_whole_replacement_and_version_checks(self) -> None:
        first = self.store.commit(
            version=1,
            stage="ingest",
            payload={"value": 1},
            completed_batches={"ingest": ["doc:1"]},
        )
        self.assertEqual(first["version"], 1)
        self.assertEqual(self.store.load()["payload"], {"value": 1})
        second = self.store.commit(
            version=2,
            stage="inverted",
            payload={"value": 2},
            completed_batches={"ingest": ["doc:1"], "inverted": ["doc:1"]},
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.store.load()["payload"], {"value": 2})

        with self.assertRaises(RecallError) as rollback:
            self.store.commit(version=1, stage="ingest", payload={"value": 0})
        self.assertEqual(rollback.exception.code, E_SNAPSHOT_ROLLBACK)

        with self.assertRaises(RecallError) as conflicting:
            self.store.commit(version=2, stage="inverted", payload={"value": 3})
        self.assertEqual(conflicting.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_corruption_is_detected(self) -> None:
        self.store.commit(version=1, stage="ingest", payload={"value": 1})
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw.replace('"value":1', '"value":9'), encoding="utf-8")
        with self.assertRaises(RecallError) as corrupt:
            self.store.load()
        self.assertEqual(corrupt.exception.code, E_SNAPSHOT_CORRUPT)

    def test_commit_lock_is_nonblocking(self) -> None:
        with nonblocking_commit_lock(self.store.lock_path):
            with self.assertRaises(RecallError) as busy:
                self.store.commit(version=1, stage="ingest", payload={"value": 1})
        self.assertEqual(busy.exception.code, E_STAGE_BUSY)

    def test_commit_lock_is_nonblocking_across_processes(self) -> None:
        code = """
import sys
from sparse_recall.errors import RecallError
from sparse_recall.snapshots import VersionedSnapshotStore
try:
    VersionedSnapshotStore(sys.argv[1], snapshot_kind="pipeline-checkpoint").commit(
        version=1, stage="ingest", payload={"value": 1}
    )
except RecallError as exc:
    print(exc.code)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)
        with nonblocking_commit_lock(self.store.lock_path):
            process = subprocess.run(
                [sys.executable, "-c", code, str(self.path)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), E_STAGE_BUSY)


if __name__ == "__main__":
    unittest.main()
