"""业务要求 5：K/V 修订与 block_size 变更的增量失效传播。"""

import unittest
from dataclasses import replace

from rope_attn.engine import EngineContext, compute_layer
from rope_attn.invalidate import (
    run_block_size_change,
    run_kv_change,
)
from rope_attn.nodes import (
    InvalidationPlan,
    node_id,
    plan_block_size_change,
    plan_kv_change,
)
from rope_attn.cache import ContentCache
from rope_attn.errors import (
    InconsistentPlanError,
    RevisionRollbackError,
)
from tests._helpers import TempWorkspace, make_task


class InvalidationPlanTest(unittest.TestCase):
    def test_kv_change_only_invalidates_downstream(self):
        task = make_task()
        new_task = replace(task, kv_revision=2)
        plan = plan_kv_change(task, new_task, 0)
        self.assertEqual(plan.change_type, "kv_content")
        self.assertIn(node_id(task.case_id, 0, "rope_k"), plan.recompute)
        self.assertIn(node_id(task.case_id, 0, "rope_q"), plan.reused)
        self.assertIn(node_id(task.case_id, 0, "angles"), plan.reused)
        self.assertNotIn(node_id(task.case_id, 0, "q"), plan.recompute)

    def test_block_size_change_keeps_rope_upstream(self):
        task = make_task(block_size=4)
        new_task = replace(task, block_size=2)
        plan = plan_block_size_change(task, new_task, 0)
        self.assertEqual(plan.change_type, "block_size")
        for upstream in ("q", "kv_k", "kv_v", "angles", "rope_q", "rope_k"):
            self.assertIn(
                node_id(task.case_id, 0, upstream), plan.reused
            )
        self.assertTrue(
            any("score_b" in node for node in plan.recompute)
        )

    def test_revision_rollback_rejected(self):
        task = make_task(kv_revision=3)
        new_task = replace(task, kv_revision=2)
        with self.assertRaises(RevisionRollbackError):
            plan_kv_change(task, new_task, 0)

    def test_partition_violation_raises_inconsistent(self):
        plan = InvalidationPlan(
            change_type="kv_content",
            affected=["a", "b"],
            recompute=["a"],
            reused=["a"],
        )
        with self.assertRaises(InconsistentPlanError) as ctx:
            plan.verify()
        self.assertEqual(ctx.exception.code, "E_INCONSISTENT")


class InvalidationExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def _prime_cache(self, task, config):
        cache = ContentCache(config.cache_dir)
        ctx = EngineContext(task, config, cache)
        compute_layer(ctx, 0)
        return cache

    def test_kv_revision_reuses_upstream_and_recomputes_suffix(self):
        task = make_task()
        config = self.workspace.config()
        self._prime_cache(task, config)
        new_task = replace(task, kv_revision=2)
        result = run_kv_change(task, new_task, [0], config)
        reused = set(result.reused_verified)
        self.assertIn(node_id(task.case_id, 0, "rope_q"), reused)
        self.assertIn(node_id(task.case_id, 0, "angles"), reused)
        recompute_set = set(result.recomputed)
        self.assertIn(node_id(new_task.case_id, 0, "rope_k"), recompute_set)
        self.assertTrue(reused.isdisjoint(recompute_set))

    def test_block_size_change_full_partition(self):
        task = make_task(block_size=4)
        config = self.workspace.config()
        self._prime_cache(task, config)
        new_task = replace(task, block_size=2)
        result = run_block_size_change(task, new_task, [0], config)
        plan = result.plan
        self.assertEqual(
            len(plan["recompute"]) + len(plan["reused"]),
            len(set(plan["recompute"]) | set(plan["reused"])),
        )
        self.assertTrue(
            any("rope_q" in node for node in result.reused_verified)
        )


if __name__ == "__main__":
    unittest.main()
