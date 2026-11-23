"""业务要求 8 补充：worker 异常归因与跨模块一致性自检。"""

import json
import unittest
from dataclasses import replace

from rope_attn.engine import run_worker
from rope_attn.errors import NumericUnstableError, WorkerFailure
from rope_attn.invalidate import run_kv_change
from rope_attn.nodes import plan_kv_change
from rope_attn.pipeline import Executor
from rope_attn.selfcheck import run_self_check
from tests._helpers import TempWorkspace, make_task


class WorkerAttributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_numeric_instability_carries_case_layer_block(self):
        task = make_task()
        # 极小容差必然触发分母还原不稳定。
        config = self.workspace.config(tolerance=1.0e-30)
        payload = {
            "task": task.to_dict(),
            "config": config.__dict__,
            "layer": 1,
            "stage": "score",
            "block": None,
        }
        with self.assertRaises(NumericUnstableError) as ctx:
            run_worker(payload)
        self.assertEqual(ctx.exception.code, "E_NUMERIC_UNSTABLE")
        self.assertEqual(ctx.exception.case_id, task.case_id)
        self.assertEqual(ctx.exception.layer, 1)
        self.assertIn("block", ctx.exception.details)

    def test_worker_failure_is_business_error(self):
        # 用 monkeypatch 让核心计算抛普通异常，验证 run_worker 在边界
        # 统一包装为带业务码、阶段、层号归因的 WorkerFailure。
        task = make_task()
        config = self.workspace.config()
        payload = {
            "task": task.to_dict(),
            "config": config.__dict__,
            "layer": 0,
            "stage": "score",
        }
        import rope_attn.engine as engine

        original = engine.compute_layer
        engine.compute_layer = lambda ctx, layer: (_ for _ in ()).throw(
            RuntimeError("boom in worker")
        )
        try:
            with self.assertRaises(WorkerFailure) as ctx:
                run_worker(payload)
        finally:
            engine.compute_layer = original
        self.assertEqual(ctx.exception.code, "E_WORKER")
        self.assertEqual(ctx.exception.case_id, task.case_id)
        self.assertEqual(ctx.exception.stage, "score")
        self.assertEqual(ctx.exception.layer, 0)


class SelfCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_full_self_check_after_pipeline_and_invalidation(self):
        task = make_task(block_size=4)
        config = self.workspace.config()
        executor = Executor([task], config)
        result = executor.run()
        self.assertEqual(result.status, "completed")

        new_task = replace(task, kv_revision=2)
        plan = plan_kv_change(task, new_task, 0)
        run_kv_change(task, new_task, [0], config)

        report = run_self_check(
            [task], config.cache_dir, config.state_dir,
            tolerance=config.tolerance, plan=plan,
        )
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))
        self.assertTrue(report["snapshot_version_stage"]["ok"])
        self.assertTrue(report["cache_key_blob"]["ok"])
        self.assertTrue(report["denominator_restore"]["ok"])
        self.assertTrue(report["invalidation_partition"]["ok"])


if __name__ == "__main__":
    unittest.main()
