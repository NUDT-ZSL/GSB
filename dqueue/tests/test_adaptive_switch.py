import os
import tempfile
import unittest

from dqueue.config import AdaptiveConfig, QueueConfig, WheelConfig
from dqueue.manifest import payload_digest
from dqueue.queue import DelayedQueue


def build_manifest(count, span_ms=20000):
    tasks = []
    for i in range(count):
        payload = {"seq": i}
        tasks.append({
            "task_id": f"bulk-{i:04d}",
            "topic": "bulk.stream",
            "fire_ms": 1000 + (i * span_ms // count),
            "policy_label": "fixed",
            "max_retries": 1,
            "payload": payload,
            "payload_digest": payload_digest(payload),
        })
    return tasks


POLICIES = [{"label": "fixed", "base_ms": 100, "multiplier": 2,
             "cap_ms": 1000, "jitter_pct": 0}]


class AdaptiveSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = QueueConfig(
            wheel=WheelConfig(tick_ms=100, ticks_per_wheel=8,
                              level_count=3),
            adaptive=AdaptiveConfig(enabled=True, switch_threshold=64,
                                    hysteresis_ratio=0.8))

    def tearDown(self):
        self.tmp.cleanup()

    def test_heap_to_wheel_switch_is_recorded_with_observations(self):
        q = DelayedQueue(os.path.join(self.tmp.name, "a.db"), self.cfg)
        q.enqueue_manifest(build_manifest(80),
                           {p["label"]: p for p in POLICIES}, 0)
        result = q.scan(500)  # nothing due yet; observation still runs
        self.assertEqual(result["mode"], "wheel")
        self.assertEqual(result["mode_version"], 2)
        log = q.status()["switch_log"]
        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["from_mode"], "heap")
        self.assertEqual(entry["to_mode"], "wheel")
        self.assertEqual(entry["before_n"], 80)
        self.assertEqual(entry["after_n"], 80)
        self.assertGreater(entry["span_ms"], 0)
        self.assertLess(entry["wheel_cost"], entry["heap_cost"] * 0.8)
        q.close()

    def test_switch_loses_nor_duplicates_due_tasks(self):
        # Same 80 tasks through forced-heap vs adaptive (which switches);
        # triggered sequences across the clock steps must be identical.
        steps = [1000, 5000, 10000, 15000, 20000, 25000]

        def run(db_name, adaptive_enabled):
            cfg = QueueConfig(
                wheel=self.cfg.wheel,
                adaptive=AdaptiveConfig(enabled=adaptive_enabled,
                                        switch_threshold=64,
                                        hysteresis_ratio=0.8))
            q = DelayedQueue(os.path.join(self.tmp.name, db_name), cfg)
            q.enqueue_manifest(build_manifest(80),
                               {p["label"]: p for p in POLICIES}, 0)
            seq = []
            for step in steps:
                seq.extend(q.scan(step)["triggered"])
            q.close()
            return seq

        heap_seq = run("heap.db", False)
        adaptive_seq = run("adaptive.db", True)
        self.assertEqual(heap_seq, adaptive_seq)
        self.assertEqual(len(set(heap_seq)), 80)
        self.assertEqual(len(set(adaptive_seq)), 80)


if __name__ == "__main__":
    unittest.main()
