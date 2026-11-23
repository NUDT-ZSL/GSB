"""Requirement 6: versioned snapshots, busy lock, rollback protection."""

from __future__ import annotations

import os
import tempfile
import unittest

from calib_workbench.errors import E_STAGE_BUSY, E_SNAPSHOT_ROLLBACK, WorkbenchError
from calib_workbench.snapshots import SnapshotStore


class TestSnapshots(unittest.TestCase):
    def test_versions_monotone_and_whole_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            p1 = store.commit("metrics", {"a": 1})
            self.assertEqual(p1["version"], 1)
            self.assertTrue(p1["changed"])
            p2 = store.commit("metrics", {"a": 1})
            self.assertEqual(p2["version"], 1)  # identical body: no-op
            self.assertFalse(p2["changed"])
            p3 = store.commit("metrics", {"a": 2})
            self.assertEqual(p3["version"], 2)  # checksum change: whole replace
            self.assertTrue(p3["changed"])
            self.assertEqual(store.read_version("metrics", 1)["body"], {"a": 1})
            self.assertEqual(store.current("metrics")["checksum"], p3["checksum"])

    def test_rollback_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            store.commit("metrics", {"a": 1})
            store.commit("metrics", {"a": 2})
            with self.assertRaises(WorkbenchError) as ctx:
                store.commit("metrics", {"a": 1}, version=1)
            self.assertEqual(ctx.exception.code, E_SNAPSHOT_ROLLBACK)
            self.assertEqual(store.current("metrics")["version"], 2)

    def test_plan_commit_busy_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            lock = os.path.join(tmp, "plan.commit.lock")
            with open(lock, "w", encoding="ascii") as fh:
                fh.write("99999")
            with self.assertRaises(WorkbenchError) as ctx:
                store.commit("plan", {"x": 1})
            self.assertEqual(ctx.exception.code, E_STAGE_BUSY)
            os.unlink(lock)
            ptr = store.commit("plan", {"x": 1})
            self.assertEqual(ptr["version"], 1)

    def test_non_plan_kinds_do_not_take_plan_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            lock = os.path.join(tmp, "plan.commit.lock")
            with open(lock, "w", encoding="ascii") as fh:
                fh.write("99999")
            ptr = store.commit("metrics", {"a": 1})  # must not raise
            self.assertEqual(ptr["version"], 1)


if __name__ == "__main__":
    unittest.main()
