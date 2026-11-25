"""要求 5：并行调度、确定性分块归并与双重资源预算裁决。"""

from __future__ import annotations

from dataclasses import replace

from sr_pipeline.errors import BusinessError, ErrorCode
from sr_pipeline.scheduler import run_scheduler

from ._base import SampleCase


class SchedulerTest(SampleCase):
    def test_inline_and_pool_same_order_and_scores(self) -> None:
        inline_config = replace(
            self.config,
            scheduler=replace(self.config.scheduler, workers=1),
        )
        inline_outputs, inline_report = run_scheduler(
            self.docs, self.queries, self.term_weights, inline_config
        )
        pool_config = replace(
            self.config,
            scheduler=replace(self.config.scheduler, workers=3),
        )
        pool_outputs, pool_report = run_scheduler(
            self.docs, self.queries, self.term_weights, pool_config
        )
        self.assertEqual(
            [item.query_id for item in inline_outputs],
            [item.query_id for item in pool_outputs],
        )
        for inline, pool in zip(inline_outputs, pool_outputs):
            self.assertEqual(
                [c.doc_id for c in inline.fusion.candidates],
                [c.doc_id for c in pool.fusion.candidates],
            )
            self.assertEqual(
                [c.final_score for c in inline.fusion.candidates],
                [c.final_score for c in pool.fusion.candidates],
            )
        self.assertTrue(
            all(batch["mode"] == "process_pool" for batch in pool_report.batches)
        )
        # 批次边界确定：batch_size=4，12 个查询 -> 3 批
        self.assertEqual([b["queries"] for b in pool_report.batches], [4, 4, 4])

    def test_recall_budget_violation(self) -> None:
        tight = replace(
            self.config,
            scheduler=replace(
                self.config.scheduler, candidate_budget=10
            ),
        )
        with self.assertRaises(BusinessError) as caught:
            run_scheduler(self.docs, self.queries, self.term_weights, tight)
        self.assertEqual(caught.exception.code, ErrorCode.RECALL_BUDGET_EXCEEDED)
        self.assertIn("candidate_budget", caught.exception.field_path)

    def test_memory_budget_violation(self) -> None:
        tight = replace(
            self.config,
            scheduler=replace(
                self.config.scheduler, memory_budget_bytes=1024
            ),
        )
        with self.assertRaises(BusinessError) as caught:
            run_scheduler(self.docs, self.queries, self.term_weights, tight)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertIn("memory_budget", caught.exception.field_path)

    def test_non_positive_budget_rejected_upfront(self) -> None:
        bad = replace(
            self.config,
            scheduler=replace(self.config.scheduler, workers=0),
        )
        with self.assertRaises(BusinessError) as caught:
            run_scheduler(self.docs, self.queries, self.term_weights, bad)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_BUDGET)
