import concurrent.futures as cf
import os
import tempfile
import unittest

from dqueue.config import QueueConfig
from dqueue.manifest import payload_digest
from dqueue.queue import DelayedQueue


def _worker_scan(db, now_ms):
    q = DelayedQueue(db, QueueConfig())
    try:
        return q.scan(now_ms)["triggered"]
    finally:
        q.close()


def _worker_ack(db, pull_id, consumer, now_ms):
    q = DelayedQueue(db, QueueConfig())
    try:
        return q.ack(pull_id, consumer, False, now_ms, "E_BUSY")
    finally:
        q.close()


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "c.db")
        self.q = DelayedQueue(self.db, QueueConfig())
        tasks = []
        for i in range(40):
            payload = {"i": i}
            tasks.append({
                "task_id": f"p-{i:03d}", "topic": "parallel.work",
                "fire_ms": 1000, "policy_label": "fixed",
                "max_retries": 5, "payload": payload,
                "payload_digest": payload_digest(payload)})
        policies = {"fixed": {"label": "fixed", "base_ms": 100,
                              "multiplier": 2, "cap_ms": 1000,
                              "jitter_pct": 0}}
        self.q.enqueue_manifest(tasks, policies, 0)
        self.q.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_scans_trigger_each_task_once(self):
        with cf.ProcessPoolExecutor(max_workers=6, mp_context=None) as ex:
            futures = [ex.submit(_worker_scan, self.db, 1000)
                       for _ in range(6)]
            triggered = []
            for fut in cf.as_completed(futures, timeout=30):
                triggered.extend(fut.result(timeout=30))
        self.assertEqual(len(triggered), 40)
        self.assertEqual(len(set(triggered)), 40)
        q = DelayedQueue(self.db, QueueConfig())
        running = q.store.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE stage='RUNNING'"
        ).fetchone()["c"]
        pulls = q.store.conn.execute(
            "SELECT COUNT(*) AS c, (SELECT COUNT(DISTINCT task_id) "
            "FROM pulls) AS d FROM pulls").fetchone()
        self.assertEqual(running, 40)
        self.assertEqual(pulls["c"], 40)
        self.assertEqual(pulls["d"], 40)
        q.close()

    def test_parallel_acks_increment_attempts_once(self):
        q = DelayedQueue(self.db, QueueConfig())
        q.scan(1000)
        polled = q.poll("parallel.work", "owner", 1000)
        pull_id = polled["tasks"][0]["pull_id"]
        task_id = polled["tasks"][0]["task_id"]
        q.close()

        with cf.ProcessPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_worker_ack, self.db, pull_id, "owner",
                                 1100 + i)
                       for i in range(5)]
            results = [fut.result(timeout=30)
                       for fut in cf.as_completed(futures, timeout=30)]

        applied = [r for r in results if not r.get("idempotent")]
        self.assertEqual(len(applied), 1)
        q = DelayedQueue(self.db, QueueConfig())
        row = q.store.conn.execute(
            "SELECT attempts, next_fire_ms FROM tasks WHERE task_id=?",
            (task_id,)).fetchone()
        self.assertEqual(row["attempts"], 1)
        # Exactly one ack applied; fixed backoff adds base 100 ms to whichever
        # logical clock won the commit race (1100..1104), so the persisted
        # next_fire_ms must be winning_clock + 100 and stay within that band.
        self.assertIn(row["next_fire_ms"], [1200, 1201, 1202, 1203, 1204])
        q.close()


if __name__ == "__main__":
    unittest.main()
