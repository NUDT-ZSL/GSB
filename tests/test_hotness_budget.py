from __future__ import annotations

import unittest

from gsb.budget import ACCELERATED_MODE, EXACT_MODE, decide_mode
from gsb.hotness import HotTracker
from gsb.models import ColumnBatch, EngineConfig


def make_batch(index: int, ordinal: int, counts: dict[str, int]) -> ColumnBatch:
    keys = []
    for key, count in counts.items():
        keys.extend([key] * count)
    return ColumnBatch(
        ordinal=ordinal,
        batch_index=index,
        row_count=len(keys),
        columns={"k": keys},
        digest=f"d{index}",
        source_path=f"b{index}",
        actual_digest=f"a{index}",
    )


def partial(counts: dict[str, int]) -> dict:
    return {
        f"s:{key}": {
            "key": key,
            "sort_key": f"2:\"{key}\"",
            "rows": count,
            "aggs": {},
        }
        for key, count in counts.items()
    }


class HotnessTests(unittest.TestCase):
    def test_streaming_change_events_keep_trigger_time_observations(self) -> None:
        tracker = HotTracker(hot_key_min_rows=5, skew_ratio=0.4)
        first = tracker.step(make_batch(1, 1, {"A": 6, "B": 4}), partial({"A": 6, "B": 4}))
        second = tracker.step(make_batch(2, 2, {"A": 6, "B": 4}), partial({"A": 6, "B": 4}))
        tracker.step(make_batch(3, 3, {"A": 6, "B": 4}), partial({"A": 6, "B": 4}))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first["trigger_batch_index"], 1)
        self.assertEqual(first["hot_keys"], ["A"])
        self.assertEqual(
            first["observations"][0]["observed_batches"],
            [{"batch_index": 1, "rows": 6}],
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["hot_keys"], ["A", "B"])
        self.assertEqual(tracker.version, 2)
        self.assertFalse(tracker.report()["criteria"]["pre_scan_used"])


class BudgetTests(unittest.TestCase):
    def config(self) -> EngineConfig:
        return EngineConfig(
            time_budget_ms=30.0,
            max_batch_rows=1000,
            hot_key_min_rows=10,
            skew_ratio=0.2,
            memory_limit_mib=1.0,
            fixed_scale=1000,
            float_tolerance=1e-12,
            logical_clock_step_ms=1.0,
            exact_cost_ms_per_1k_rows=10.0,
            accelerated_cost_ms_per_1k_rows=1.0,
            switch_overhead_ms=0.5,
        )

    def test_switch_is_deterministic_and_charges_overhead(self) -> None:
        rows = {1: 1000, 2: 1000, 3: 1000}
        first = decide_mode(self.config(), EXACT_MODE, 0.0, 1, rows, False)
        second = decide_mode(self.config(), EXACT_MODE, 0.0, 1, rows, False)
        self.assertEqual(first.mode, ACCELERATED_MODE)
        self.assertTrue(first.switched)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertAlmostEqual(
            first.elapsed_after_ms,
            0.5 + 1.0 + 1.0,
            places=12,
        )
        self.assertIn(EXACT_MODE, first.rejected_modes)


if __name__ == "__main__":
    unittest.main()

