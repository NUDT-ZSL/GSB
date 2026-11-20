import os
import tempfile
import unittest

from dqueue.config import AdaptiveConfig, QueueConfig, WheelConfig
from dqueue.errors import DQError, ErrorCode
from dqueue.manifest import load_files
from dqueue.queue import (STAGE_DEAD, STAGE_SCHEDULED, STAGE_SUCCEEDED,
                          DelayedQueue)

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")


class QueueFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "q.db")
        self.cfg = QueueConfig(
            lease_timeout_ms=5000,
            wheel=WheelConfig(tick_ms=100, ticks_per_wheel=8,
                              level_count=3),
            adaptive=AdaptiveConfig(enabled=True, switch_threshold=64,
                                    hysteresis_ratio=0.8))
        self.q = DelayedQueue(self.db, self.cfg)
        tasks, policies = load_files(
            os.path.join(SAMPLES, "manifest.json"),
            os.path.join(SAMPLES, "policies.json"))
        self.tasks, self.policies = tasks, policies
        self.q.enqueue_manifest(tasks, policies, 0)

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def test_enqueue_is_rejected_on_second_identical_manifest(self):
        with self.assertRaises(DQError) as ctx:
            self.q.enqueue_manifest(self.tasks, self.policies, 1)
        self.assertEqual(ctx.exception.code, ErrorCode.E_TASK_DUPLICATE)

    def test_invalid_manifest_creates_no_database_file(self):
        from dqueue.cli import run as cli_run
        target = os.path.join(self.tmp.name, "never", "q.db")
        bad_manifest = os.path.join(self.tmp.name, "bad.json")
        import json as _json
        broken = _json.loads(_json.dumps(self.tasks))
        broken[0]["payload_digest"] = "tampered"
        with open(bad_manifest, "w") as fh:
            _json.dump(broken, fh)
        policies_path = os.path.join(SAMPLES, "policies.json")
        code = cli_run([
            "--db", target,
            "--config", os.path.join(SAMPLES, "queue_config.json"),
            "enqueue", "--manifest", bad_manifest,
            "--policies", policies_path, "--now", "0"])
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(target))

    def test_full_flow_success_and_retry_dead_letter(self):
        # t-001 succeeds; t-007 has max_retries=0 and dies on first failure.
        scan = self.q.scan(1000)
        self.assertEqual(scan["triggered"], ["t-001", "t-002"])
        polled = self.q.poll("orders.created", "c1", 1000)
        pulls = {t["task_id"]: t["pull_id"] for t in polled["tasks"]}
        ok = self.q.ack(pulls["t-001"], "c1", True, 1100)
        self.assertFalse(ok["idempotent"])
        row = self.q.store.conn.execute(
            "SELECT stage FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(row["stage"], STAGE_SUCCEEDED)

        self.q.scan(3100)
        polled = self.q.poll("orders.created", "c1", 3100)
        pull_007 = next(t for t in polled["tasks"]
                        if t["task_id"] == "t-007")
        self.q.ack(pull_007["pull_id"], "c1", False, 3200, "E_DOWNSTREAM")
        row = self.q.store.conn.execute(
            "SELECT * FROM tasks WHERE task_id='t-007'").fetchone()
        self.assertEqual(row["stage"], STAGE_DEAD)
        self.assertEqual(row["dead_reason"], "RETRY_EXHAUSTED")
        self.assertEqual(row["last_error_code"], "E_DOWNSTREAM")
        self.assertEqual(row["attempts"], 1)

        # Dead letters are never scheduled again.
        scan = self.q.scan(20000)
        self.assertNotIn("t-007", scan["triggered"])

    def test_retry_schedules_backoff_and_attempts_are_persisted(self):
        self.q.scan(1000)
        polled = self.q.poll("orders.created", "c1", 1000)
        pull_id = next(t["pull_id"] for t in polled["tasks"]
                       if t["task_id"] == "t-001")
        result = self.q.ack(pull_id, "c1", False, 1200, "E_TRANSIENT")
        self.assertFalse(result["idempotent"])
        row = self.q.store.conn.execute(
            "SELECT * FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(row["stage"], STAGE_SCHEDULED)
        self.assertEqual(row["attempts"], 1)
        # fixed policy: base 500, no jitter -> 1200 + 500 = 1700.
        self.assertEqual(row["next_fire_ms"], 1700)
        before = row["next_fire_ms"]

        # The requeued task triggers again and fails; second retry = +1000.
        self.q.scan(1700)
        polled = self.q.poll("orders.created", "c1", 1700)
        pull2 = next(t["pull_id"] for t in polled["tasks"]
                     if t["task_id"] == "t-001")
        self.q.ack(pull2, "c1", False, 1800, "E_TRANSIENT")
        row = self.q.store.conn.execute(
            "SELECT * FROM tasks WHERE task_id='t-001'").fetchone()
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["next_fire_ms"], 2800)
        self.assertNotEqual(before, row["next_fire_ms"])

    def test_ack_idempotent_and_foreign_consumer(self):
        self.q.scan(1000)
        polled = self.q.poll("orders.created", "c1", 1000)
        pull_id = polled["tasks"][0]["pull_id"]
        first = self.q.ack(pull_id, "c1", True, 1100)
        second = self.q.ack(pull_id, "c1", True, 1200)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

        self.q.scan(1500)
        polled = self.q.poll("orders.paid", "c2", 1500)
        other = next(t for t in polled["tasks"]
                     if t["task_id"] == "t-003")["pull_id"]
        with self.assertRaises(DQError) as ctx:
            self.q.ack(other, "intruder", True, 1600)
        self.assertEqual(ctx.exception.code, ErrorCode.E_FOREIGN_CONSUMER)

    def test_unknown_pull_and_missing_task_error_codes(self):
        with self.assertRaises(DQError) as ctx:
            self.q.ack("nope", "c1", True, 1000)
        self.assertEqual(ctx.exception.code, ErrorCode.E_PULL_NOT_FOUND)
        with self.assertRaises(DQError) as ctx:
            self.q._task_row("ghost")
        self.assertEqual(ctx.exception.code, ErrorCode.E_TASK_NOT_FOUND)

    def test_lease_timeout_requeues_from_persisted_state(self):
        self.q.scan(1000)
        polled = self.q.poll("orders.created", "c1", 1000)
        pull_id = polled["tasks"][0]["pull_id"]
        # Lease is 5000 ms: at 6000 the task must be requeued, attempts kept.
        result = self.q.scan(6000)
        self.assertIn(polled["tasks"][0]["task_id"],
                      result["requeued_timeouts"])
        # Late ack against the timed-out pull is ignored, never double-applied.
        late = self.q.ack(pull_id, "c1", False, 6100, "E_LATE")
        self.assertTrue(late.get("ignored_late"))
        row = self.q.store.conn.execute(
            "SELECT attempts FROM tasks WHERE task_id=?",
            (polled["tasks"][0]["task_id"],)).fetchone()
        self.assertEqual(row["attempts"], 0)

    def test_clock_rollback_is_rejected(self):
        self.q.scan(2000)
        with self.assertRaises(DQError) as ctx:
            self.q.scan(1999)
        self.assertEqual(ctx.exception.code, ErrorCode.E_CLOCK_ROLLBACK)

    def test_mode_version_rollback_rejected(self):
        v1 = self.q.set_mode("wheel", 100)["version"]
        with self.assertRaises(DQError) as ctx:
            self.q.set_mode("heap", 200, version=v1)
        self.assertEqual(
            ctx.exception.code, ErrorCode.E_MODE_VERSION_ROLLBACK)

    def test_verify_detects_drift_and_status_is_sorted(self):
        self.assertTrue(self.q.verify()["ok"])
        self.q.store.conn.execute(
            "UPDATE tasks SET payload_digest='x' WHERE task_id='t-001'")
        with self.assertRaises(DQError) as ctx:
            self.q.verify()
        self.assertEqual(ctx.exception.code, ErrorCode.E_VERIFY)
        status = self.q.status()
        ids = [t["task_id"] for t in status["tasks"]]
        self.assertEqual(ids, sorted(ids))

    def test_wheel_mode_produces_same_triggered_sequence(self):
        self.q.set_mode("wheel", 1)
        steps = [1000, 1500, 2200, 2600, 3100, 3200, 4000, 4200, 5000,
                 5600, 8000]
        wheel_seq = []
        for step in steps:
            wheel_seq.extend(self.q.scan(step)["triggered"])
        # Compare with a fresh heap-driven queue on the same manifest.
        db2 = os.path.join(self.tmp.name, "heap.db")
        heap_q = DelayedQueue(db2, self.cfg)
        heap_q.enqueue_manifest(self.tasks, self.policies, 0)
        heap_seq = []
        for step in steps:
            heap_seq.extend(heap_q.scan(step)["triggered"])
        heap_q.close()
        self.assertEqual(wheel_seq, heap_seq)


if __name__ == "__main__":
    unittest.main()
