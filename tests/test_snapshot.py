import json
import tempfile
import unittest
from pathlib import Path

from sampling_bench.errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK
from sampling_bench.snapshot import SnapshotStore


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_monotonic_and_atomic_replace(self):
        self.store.write("samples", {"v": 1}, expected_next_version=1)
        self.store.write("samples", {"v": 2}, expected_next_version=2)
        snapshot = self.store.read_current("samples")
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.payload, {"v": 2})

    def test_rollback_rejected(self):
        self.store.write("ledger", {"v": 1}, expected_next_version=1)
        with self.assertRaises(Exception) as ctx:
            self.store.write("ledger", {"v": 0}, expected_next_version=1)
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_version_jump_rejected(self):
        self.store.write("ledger", {"v": 1}, expected_next_version=1)
        with self.assertRaises(Exception) as ctx:
            self.store.write("ledger", {"v": 3}, expected_next_version=3)
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_corrupt_checksum_detected(self):
        self.store.write("decisions", {"v": 1}, expected_next_version=1)
        path = self.store.path_for("decisions")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["checksum"] = "deadbeef"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self.store.read_current("decisions")
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(ctx.exception.detail["version"], 1)

    def test_malformed_json_detected(self):
        self.store.write("decisions", {"v": 1}, expected_next_version=1)
        path = self.store.path_for("decisions")
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self.store.read_current("decisions")
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()
