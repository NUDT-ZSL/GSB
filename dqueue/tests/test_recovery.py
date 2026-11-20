import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from dqueue.config import QueueConfig
from dqueue.manifest import load_files
from dqueue.queue import (STAGE_RUNNING, STAGE_SCHEDULED,
                          STAGE_SUCCEEDED, DelayedQueue)

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")


CRASH_SCRIPT = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {src!r})
    from dqueue.queue import DelayedQueue
    from dqueue.manifest import load_files

    db, manifest, policies, point, case = sys.argv[1:6]
    tasks, pol = load_files(manifest, policies)

    def hook(where):
        if where == point:
            os._exit(99)

    q = DelayedQueue(db, crash_hook=hook)
    if case == "fresh":
        q.enqueue_manifest(tasks, pol, 0)
    if point.startswith("enqueue"):
        raise SystemExit(0)
    q.scan(1000)
    if point.startswith("scan"):
        raise SystemExit(0)
    polled = q.poll("orders.created", "c1", 1000)
    pull_id = polled["tasks"][0]["pull_id"]
    q.ack(pull_id, "c1", True, 1100)
""")

ACK_CRASH_SCRIPT = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {src!r})
    from dqueue.queue import DelayedQueue

    db = sys.argv[1]

    def hook(where):
        if where == "ack.before_commit":
            os._exit(99)

    q = DelayedQueue(db, crash_hook=hook)
    polled = q.poll("orders.created", "c1", 3100)
    pull_id = next(t["pull_id"] for t in polled["tasks"]
                   if t["task_id"] == "t-001")
    q.ack(pull_id, "c1", True, 3200)
""")


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.manifest = os.path.join(SAMPLES, "manifest.json")
        self.policies = os.path.join(SAMPLES, "policies.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _crash(self, db_name, point, case="fresh"):
        db = os.path.join(self.tmp.name, db_name)
        script = CRASH_SCRIPT.format(src=self.src)
        proc = subprocess.run(
            [sys.executable, "-c", script, db, self.manifest,
             self.policies, point, case],
            capture_output=True, text=True)
        return db, proc

    def test_crash_during_enqueue_writes_nothing(self):
        db, proc = self._crash("enqueue.db", "enqueue.mid")
        self.assertEqual(proc.returncode, 99, proc.stderr)
        q = DelayedQueue(db, QueueConfig())
        count = q.store.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        snap = q.store.conn.execute(
            "SELECT COUNT(*) AS c FROM manifest_snapshot").fetchone()["c"]
        self.assertEqual((count, snap), (0, 0))
        # Re-enqueue after recovery must succeed (no partial unique state).
        tasks, pol = load_files(self.manifest, self.policies)
        q.enqueue_manifest(tasks, pol, 0)
        self.assertEqual(q.verify()["checked_tasks"], 12)
        q.close()

    def test_crash_after_pull_insert_before_stage_update_rolls_back(self):
        db, proc = self._crash("scan1.db",
                               "scan.after_pull_insert")
        self.assertEqual(proc.returncode, 99, proc.stderr)
        q = DelayedQueue(db, QueueConfig())
        q.resume(1200)
        # The transaction rolled back: no pull, task still SCHEDULED.
        pulls = q.store.conn.execute(
            "SELECT COUNT(*) AS c FROM pulls").fetchone()["c"]
        stage = q.store.conn.execute(
            "SELECT stage FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(pulls, 0)
        self.assertEqual(stage["stage"], STAGE_SCHEDULED)
        # Replay scan triggers exactly once.
        triggered = q.scan(1200)["triggered"]
        self.assertEqual(triggered.count("t-001"), 1)
        q.close()

    def test_crash_after_stage_update_keeps_durable_trigger(self):
        db, proc = self._crash("scan2.db",
                               "scan.after_commit")
        self.assertEqual(proc.returncode, 99, proc.stderr)
        q = DelayedQueue(db, QueueConfig())
        q.resume(1200)
        stage = q.store.conn.execute(
            "SELECT stage FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(stage["stage"], STAGE_RUNNING)
        # No duplicate trigger on the next scan.
        triggered = q.scan(2000)["triggered"]
        self.assertNotIn("t-001", triggered)
        # The existing pull remains leasable and can be acknowledged once.
        polled = q.poll("orders.created", "c1", 2000)
        pull_id = next(t["pull_id"] for t in polled["tasks"]
                       if t["task_id"] == "t-001")
        q.ack(pull_id, "c1", True, 2100)
        again = q.ack(pull_id, "c1", True, 2200)
        self.assertTrue(again["idempotent"])
        stage = q.store.conn.execute(
            "SELECT stage FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(stage["stage"], STAGE_SUCCEEDED)
        q.close()

    def test_crash_during_ack_does_not_double_apply(self):
        db, proc = self._crash("ack0.db", "scan.after_commit")
        self.assertEqual(proc.returncode, 99, proc.stderr)
        q = DelayedQueue(db, QueueConfig())
        q.resume(1200)
        q.close()

        # Separate process leases the task and is SIGKILLed inside the ack
        # transaction, before COMMIT.
        proc = subprocess.run(
            [sys.executable, "-c",
             ACK_CRASH_SCRIPT.format(src=self.src), db],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 99, proc.stderr)

        q = DelayedQueue(db, QueueConfig())
        q.resume(3600)
        # Ack never committed: attempts unchanged, pull still open or requeued.
        row = q.store.conn.execute(
            "SELECT attempts, stage FROM tasks WHERE task_id='t-001'"
        ).fetchone()
        self.assertEqual(row["attempts"], 0)
        self.assertIn(row["stage"], (STAGE_SCHEDULED, STAGE_RUNNING))
        # A later real ack commits exactly once.
        if row["stage"] == STAGE_RUNNING:
            pull_id = q.store.conn.execute(
                "SELECT pull_id FROM pulls WHERE task_id='t-001' "
                "AND status='OPEN'").fetchone()["pull_id"]
            q.ack(pull_id, "c1", True, 3700)
        else:
            q.scan(9000)  # lease timeout requeue, then retrigger
            polled = q.poll("orders.created", "c1", 9000)
            pull_id = next(t["pull_id"] for t in polled["tasks"]
                           if t["task_id"] == "t-001")
            q.ack(pull_id, "c1", True, 9100)
        row = q.store.conn.execute(
            "SELECT stage, attempts FROM tasks WHERE task_id='t-001'"
        ).fetchone()
        self.assertEqual(row["stage"], STAGE_SUCCEEDED)
        self.assertEqual(row["attempts"], 0)
        q.close()


if __name__ == "__main__":
    unittest.main()
