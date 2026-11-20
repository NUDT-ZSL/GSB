"""Heap and timing-wheel must emit byte-identical expiration sequences."""

import unittest

from dqueue.config import WheelConfig
from dqueue.scheduler import (HeapScheduler, WheelScheduler, choose_mode,
                              estimate_cost)


def drive(scheduler, entries, steps):
    for fire_ms, task_id in entries:
        scheduler.add(fire_ms, task_id, 1)
    sequences = []
    for now_ms in steps:
        due = scheduler.advance(now_ms)
        sequences.append((now_ms, [(a, b) for a, b, _ in due]))
    return sequences


class SchedulerEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.cfg = WheelConfig(tick_ms=100, ticks_per_wheel=8, level_count=3)

    def test_identical_on_sample_like_stream(self):
        entries = [
            (1000, "t-001"), (1000, "t-002"), (1500, "t-003"),
            (1500, "t-004"), (2200, "t-005"), (2600, "t-006"),
            (3100, "t-007"), (3200, "t-008"), (4000, "t-009"),
            (4200, "t-010"), (5000, "t-011"), (5600, "t-012"),
        ]
        steps = [0, 1000, 1500, 2200, 2600, 3100, 3200, 4000, 4200,
                 5000, 5600, 8000]
        heap_seq = drive(HeapScheduler(), entries, steps)
        wheel_seq = drive(WheelScheduler(self.cfg, 0), entries, steps)
        self.assertEqual(heap_seq, wheel_seq)

    def test_identical_with_unaligned_and_out_of_order_inserts(self):
        entries = [
            (51234, "z"), (120, "a"), (99, "b"), (6401, "c"),
            (51200, "d"), (6400, "e"), (101, "f"), (70000, "g"),
            (12345, "h"), (333, "i"), (6399, "j"), (6500, "k"),
        ]
        steps = [50, 100, 6400, 20000, 51234, 75000]
        heap_seq = drive(HeapScheduler(), entries, steps)
        wheel_seq = drive(WheelScheduler(self.cfg, 0), entries, steps)
        self.assertEqual(heap_seq, wheel_seq)

    def test_identical_across_wheel_horizon(self):
        # Horizon = 100 * 8^3 = 51200 ms; include far-future entries.
        entries = [(1000, "near"), (51199, "edge"), (51200, "over"),
                   (120000, "far"), (300000, "very_far")]
        steps = [1000, 51199, 51200, 120000, 300000]
        heap_seq = drive(HeapScheduler(), entries, steps)
        wheel_seq = drive(WheelScheduler(self.cfg, 0), entries, steps)
        self.assertEqual(heap_seq, wheel_seq)

    def test_tie_break_is_task_id_order(self):
        entries = [(1000, "z"), (1000, "a"), (1000, "m")]
        steps = [1000]
        expected = [(1000, [(1000, "a"), (1000, "m"), (1000, "z")])]
        self.assertEqual(drive(HeapScheduler(), entries, steps), expected)
        self.assertEqual(
            drive(WheelScheduler(self.cfg, 0), entries, steps), expected)

    def test_large_random_stream_matches(self):
        import random
        rng = random.Random(20261121)
        entries = [(rng.randint(0, 200000), f"id-{i:05d}")
                   for i in range(500)]
        steps = sorted({rng.randint(0, 200000) for _ in range(40)})
        heap_seq = drive(HeapScheduler(), entries, steps)
        wheel_seq = drive(WheelScheduler(self.cfg, 0), entries, steps)
        self.assertEqual(heap_seq, wheel_seq)

    def test_adaptive_decision_and_hysteresis(self):
        from dqueue.config import AdaptiveConfig
        adaptive = AdaptiveConfig(enabled=True, switch_threshold=64,
                                  hysteresis_ratio=0.8)
        # Small scale stays on heap regardless of span.
        mode, reason, _ = choose_mode(10, 100000, self.cfg, adaptive, "heap")
        self.assertEqual(mode, "heap")
        self.assertIn("threshold", reason)
        # Large scale, wide span: wheel becomes cheaper.
        mode, reason, costs = choose_mode(
            200, 100000, self.cfg, adaptive, "heap")
        self.assertEqual(mode, "wheel")
        self.assertLess(costs["wheel"], costs["heap"])
        # Hysteresis keeps wheel while n stays above the threshold even if the
        # span temporarily narrows.
        mode, _, _ = choose_mode(200, 100000, self.cfg, adaptive, "wheel")
        self.assertEqual(mode, "wheel")
        mode, _, _ = choose_mode(200, 100, self.cfg, adaptive, "wheel")
        self.assertEqual(mode, "wheel")
        # Falling below the threshold switches back to the precise heap.
        mode, reason, costs = choose_mode(
            63, 0, self.cfg, adaptive, "wheel")
        self.assertEqual(mode, "heap")
        self.assertIn("threshold", reason)

    def test_cost_estimates_are_deterministic(self):
        c1 = estimate_cost("wheel", 100, 5000, self.cfg)
        c2 = estimate_cost("wheel", 100, 5000, self.cfg)
        self.assertEqual(c1, c2)


if __name__ == "__main__":
    unittest.main()
