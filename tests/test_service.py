"""服务编排测试: 首块耗时预算 / 并发冲突 / 资源上限 / 切网格一致性."""

from __future__ import annotations

import threading
import time
import unittest

from genapp.config import ResourceLimits
from genapp.errors import (
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    GenError,
)
from genapp.service import STAGE_ITERATE, GenerationService

from .helpers import TOKENS, small_cfg, temp_store


class TestService(unittest.TestCase):
    def test_run_completes_and_ready_blocks_queryable(self):
        tmp, store = temp_store()
        with tmp:
            svc = GenerationService(small_cfg(), store, TOKENS)
            result = svc.run()
            ready = svc.ready_blocks()
            self.assertEqual(len(ready["ready"]), result.plan["blocks"])
            first = ready["ready"][0]
            data = svc.preview_block(first["block_id"])
            self.assertTrue(len(data) > 0)
            self.assertEqual(result.consistency["verdict"], "identical")

    def test_grid_switch_rerun_byte_identical(self):
        tmp, store = temp_store()
        with tmp:
            svc = GenerationService(small_cfg(), store, TOKENS)
            r1 = svc.run(grid="1x1")
            r2 = svc.run(grid="4x4")
            self.assertEqual(r1.image, r2.image)

    def test_latency_budget_error_carries_block_ids(self):
        tmp, store = temp_store()
        with tmp:
            svc = GenerationService(
                small_cfg(latency_budget_ms=0.0), store, TOKENS
            )
            with self.assertRaises(GenError) as ctx:
                svc.run(grid="4x4")
            exc = ctx.exception
            self.assertEqual(exc.code, E_LATENCY_BUDGET)
            self.assertEqual(len(exc.details["done_blocks"]), 1)
            self.assertEqual(len(exc.details["pending_blocks"]), 15)

    def test_stage_busy_on_concurrent_commit(self):
        tmp, store = temp_store()
        with tmp:
            svc = GenerationService(small_cfg(), store, TOKENS)
            errors = []
            results = []

            def worker():
                try:
                    results.append(svc.run())
                except GenError as exc:
                    errors.append(exc.code)

            t = threading.Thread(target=worker)
            t.start()
            deadline = time.time() + 10.0
            while svc.stage != STAGE_ITERATE and time.time() < deadline:
                time.sleep(0.001)
            try:
                svc.run()
            except GenError as exc:
                errors.append(exc.code)
            t.join()
            self.assertIn(E_STAGE_BUSY, errors)
            self.assertEqual(len(results), 1)

    def test_concurrent_task_limit(self):
        tmp, store = temp_store()
        with tmp:
            cfg = small_cfg(
                limits=ResourceLimits(
                    max_total_pixels=1 << 20,
                    max_block_bytes=1 << 22,
                    max_concurrent_tasks=1,
                )
            )
            svc = GenerationService(cfg, store, TOKENS)
            acquired = svc._task_sem.acquire(blocking=False)
            self.assertTrue(acquired)
            try:
                with self.assertRaises(GenError) as ctx:
                    svc.run()
                self.assertEqual(ctx.exception.code, E_RESOURCE_LIMIT)
            finally:
                svc._task_sem.release()

    def test_concurrent_previews(self):
        tmp, store = temp_store()
        with tmp:
            svc = GenerationService(small_cfg(), store, TOKENS)
            result = svc.run(grid="4x4")
            got = []

            def preview(bid):
                got.append(svc.preview_block(bid))

            threads = [
                threading.Thread(target=preview, args=(i,)) for i in range(16)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(got), 16)
            self.assertEqual(result.consistency["verdict"], "identical")


if __name__ == "__main__":
    unittest.main()
