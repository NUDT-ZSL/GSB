import threading
import unittest

from asr_gov.errors import SnapshotRollback, SnapshotCorrupt, StageBusy
from asr_gov.snapshots import SnapshotStore


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.store = SnapshotStore()

    def test_commit_and_whole_replace(self):
        self.store.commit(self.store.stage("cfg", 1, b"v1-payload"))
        snap = self.store.commit(self.store.stage("cfg", 2, b"v2-payload"))
        self.assertEqual(self.store.get("cfg").payload, b"v2-payload")
        self.assertEqual(snap.version, 2)
        self.assertEqual(self.store.versions(), {"cfg": 2})

    def test_rollback_rejected(self):
        self.store.commit(self.store.stage("cfg", 2, b"newer"))
        with self.assertRaises(SnapshotRollback):
            self.store.commit(self.store.stage("cfg", 2, b"same-version"))
        with self.assertRaises(SnapshotRollback):
            self.store.commit(self.store.stage("cfg", 1, b"older"))
        self.assertEqual(self.store.get("cfg").payload, b"newer")

    def test_corrupt_checksum_rejected(self):
        staged = self.store.stage("cfg", 1, b"payload")
        staged.checksum = "0" * 64
        with self.assertRaises(SnapshotCorrupt):
            self.store.commit(staged)

    def test_tampered_storage_detected(self):
        self.store.commit(self.store.stage("cfg", 1, b"payload"))
        snap = self.store._snapshots["cfg"]
        object.__setattr__(snap, "payload", b"tampered")
        with self.assertRaises(SnapshotCorrupt):
            self.store.get("cfg")

    def test_stage_busy_nonblocking(self):
        self.store._commit_lock.acquire()
        try:
            with self.assertRaises(StageBusy):
                self.store.commit(self.store.stage("cfg", 1, b"x"))
        finally:
            self.store._commit_lock.release()
        self.store.commit(self.store.stage("cfg", 1, b"x"))

    def test_concurrent_commits_final_state_consistent(self):
        results = []
        lock = threading.Lock()

        def worker(version):
            try:
                self.store.commit(self.store.stage("cfg", version, b"p"))
                outcome = "ok"
            except StageBusy:
                outcome = "busy"
            except SnapshotRollback:
                outcome = "rollback"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(v,)) for v in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.get("cfg").version, 8)
        self.assertIn("ok", results)
        self.assertTrue(set(results) <= {"ok", "busy", "rollback"})


if __name__ == "__main__":
    unittest.main()
