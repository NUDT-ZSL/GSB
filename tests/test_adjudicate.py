"""裁决规则与计划可复现性测试."""

from __future__ import annotations

import unittest

from genapp.adjudicate import adjudicate, canonical_bytes, override_plan
from genapp.config import ResourceLimits
from genapp.errors import E_RESOURCE_LIMIT, GenError

from .helpers import small_cfg


class TestAdjudicate(unittest.TestCase):
    def test_plan_byte_reproducible(self):
        cfg = small_cfg()
        plan1 = adjudicate(cfg)
        plan2 = adjudicate(cfg)
        self.assertEqual(canonical_bytes(plan1), canonical_bytes(plan2))

    def test_coarsest_grid_within_budget_selected(self):
        cfg = small_cfg()
        plan = adjudicate(cfg)
        self.assertEqual(plan["grid"], "1x1")
        self.assertIsNone(plan["degradation"])

    def test_tight_budget_degrades_to_finest_grid(self):
        cfg = small_cfg(latency_budget_ms=0.0001)
        plan = adjudicate(cfg)
        self.assertEqual(plan["grid"], "4x4")
        self.assertEqual(plan["degradation"], "LATENCY_BUDGET")

    def test_tier2_when_coupling_exceeds_threshold(self):
        cfg = small_cfg(coupling_threshold=1, latency_budget_ms=0.0001)
        plan = adjudicate(cfg)
        self.assertEqual(plan["verify_tier"], 2)

    def test_total_pixels_hard_limit(self):
        tight = small_cfg(
            limits=ResourceLimits(
                max_total_pixels=10,
                max_block_bytes=1 << 22,
                max_concurrent_tasks=4,
            )
        )
        with self.assertRaises(GenError) as ctx:
            adjudicate(tight)
        self.assertEqual(ctx.exception.code, E_RESOURCE_LIMIT)

    def test_block_memory_limit_excludes_coarse_grids(self):
        cfg = small_cfg(
            limits=ResourceLimits(
                max_total_pixels=1 << 20,
                max_block_bytes=10000,
                max_concurrent_tasks=4,
            )
        )
        plan = adjudicate(cfg)
        self.assertEqual(plan["grid"], "4x4")

    def test_override_plan_marks_manual(self):
        cfg = small_cfg()
        plan = override_plan(cfg, "2x2")
        self.assertEqual(plan["grid"], "2x2")
        self.assertIn("manual", str(plan["rationale"]))


if __name__ == "__main__":
    unittest.main()
