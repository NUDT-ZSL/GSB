import json
import os
import tempfile
import unittest

from llm_workbench.errors import ErrorCode, WorkbenchError
from llm_workbench.snapshots import SnapshotStore, checksum_of


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_roundtrip(self):
        meta = self.store.put("artifacts", 1, {"a": 1})
        rec = self.store.get("artifacts")
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["payload"], {"a": 1})
        self.assertEqual(rec["checksum"], meta["checksum"])

    def test_wholesale_replace_on_change(self):
        self.store.put("ledger", 1, {"x": [1, 2, 3]})
        self.store.put("ledger", 2, {"y": "entirely-new"})
        rec = self.store.get("ledger")
        self.assertEqual(rec["version"], 2)
        self.assertEqual(rec["payload"], {"y": "entirely-new"})

    def test_rollback_rejected(self):
        self.store.put("decisions", 3, {"v": 3})
        with self.assertRaises(WorkbenchError) as ctx:
            self.store.put("decisions", 2, {"v": 2})
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_ROLLBACK)
        self.assertEqual(ctx.exception.context["snapshot_type"], "decisions")
        # newer version still intact
        self.assertEqual(self.store.get("decisions")["version"], 3)

    def test_corrupt_checksum_detected(self):
        self.store.put("artifacts", 5, {"k": "v"})
        path = os.path.join(self.tmp.name, "snapshot_artifacts.json")
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["payload"]["k"] = "tampered"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        with self.assertRaises(WorkbenchError) as ctx:
            self.store.get("artifacts")
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)
        self.assertEqual(ctx.exception.context["snapshot_type"], "artifacts")
        self.assertEqual(ctx.exception.context["version"], 5)

    def test_unreadable_file_is_corrupt(self):
        path = os.path.join(self.tmp.name, "snapshot_ledger.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(WorkbenchError) as ctx:
            self.store.get("ledger")
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)

    def test_put_next_monotonic(self):
        m1 = self.store.put_next("artifacts", {"n": 1})
        m2 = self.store.put_next("artifacts", {"n": 2})
        self.assertEqual((m1["version"], m2["version"]), (1, 2))

    def test_checksum_stable(self):
        self.assertEqual(checksum_of({"b": 1, "a": 2}), checksum_of({"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
