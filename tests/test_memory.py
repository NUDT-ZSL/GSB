from __future__ import annotations

import unittest

from anim_replay.concurrency import precompute_clip_cache
from anim_replay.engine import build_replay
from anim_replay.errors import BudgetError
from anim_replay.persistence import output_digest

from tests.helpers import sample_loaded


class MemoryBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loaded = sample_loaded()

    def test_peak_respects_budget_and_workers_do_not_change_output(self) -> None:
        digests = set()
        for workers in (1, 2, 4):
            cache, report = precompute_clip_cache(
                self.loaded.spec,
                max_workers=workers,
                memory_budget_bytes=384 * 1024,
            )
            self.assertGreater(len(cache), 0)
            self.assertLessEqual(report["peak_inflight_bytes"], 384 * 1024)
            self.assertGreater(report["retained_pose_bytes"], 0)
            result = build_replay(
                self.loaded.spec,
                max_workers=workers,
                memory_budget_bytes=384 * 1024,
            )
            digests.add(output_digest(result))
        self.assertEqual(len(digests), 1)

    def test_impossible_budget_raises_structured_error(self) -> None:
        with self.assertRaises(BudgetError) as context:
            precompute_clip_cache(
                self.loaded.spec,
                max_workers=2,
                memory_budget_bytes=100,
            )
        self.assertEqual(context.exception.code, "E_BUDGET")


if __name__ == "__main__":
    unittest.main()

