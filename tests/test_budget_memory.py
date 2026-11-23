import unittest

from recall_fusion.budget import AdmissionController
from recall_fusion.config import FusionConfig
from recall_fusion.errors import ErrorCode
from recall_fusion.memory import (
    EvictionEvent,
    ResidentMemoryManager,
    result_resident_bytes,
    result_total_bytes,
)
from recall_fusion.models import (
    Candidate,
    FusedCandidate,
    FusionDetail,
    MergedCandidate,
    ChannelHit,
    QueryResult,
)
from recall_fusion.sample_data import generate_documents, generate_queries


def build_result(query_id: str, bulk: int) -> QueryResult:
    candidates = {
        "inverted": [
            Candidate(f"doc_{i:04d}", "inverted", 1.0 / (i + 1))
            for i in range(bulk)
        ]
    }
    merged = [
        MergedCandidate(
            doc_id=f"doc_{i:04d}",
            hits=[ChannelHit("inverted", 1.0 / (i + 1))],
            kept_channel="inverted",
            kept_raw_score=1.0 / (i + 1),
        )
        for i in range(bulk)
    ]
    fused = [
        FusedCandidate(
            doc_id=f"doc_{i:04d}",
            final_score=1.0 / (i + 1),
            kept_channel="inverted",
            details=[
                FusionDetail("inverted", 1.0 / (i + 1), 1.0, 1.0, 1.0)
            ],
            all_hits=[ChannelHit("inverted", 1.0 / (i + 1))],
        )
        for i in range(bulk)
    ]
    return QueryResult(
        query_id=query_id,
        batch_id=0,
        channel_candidates=candidates,
        merged=merged,
        fused=fused,
        per_channel_caps={"inverted": bulk},
        cost_recall=bulk,
        cost_fusion=bulk,
        two_stage=False,
    )


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.docs = generate_documents(80, seed=42)
        self.queries = generate_queries(12, seed=42)

    def test_recall_budget_rejection_is_structured_and_excluded(self):
        config = FusionConfig.default()
        config.budgets.total_candidates = 30
        config.budgets.fusion_comparisons = 100_000
        controller = AdmissionController(self.docs, config)
        admitted, rejected = [], []
        for query in self.queries:
            record = controller.admit(query, config.scheduler.batch_size)
            (admitted if record.admitted else rejected).append(record)
        self.assertTrue(rejected)
        self.assertTrue(
            all(r.rejection_code == ErrorCode.E_RECALL_BUDGET for r in rejected)
        )
        report = controller.rejection_report()
        self.assertEqual(
            set(row["query_id"] for row in report),
            set(r.query_id for r in rejected),
        )
        self.assertIn("remaining_budget", report[0])
        self.assertEqual(
            set(r.query_id for r in rejected).intersection(
                r.query_id for r in controller.records if r.admitted
            ),
            set(),
        )

    def test_fusion_budget_rejection(self):
        config = FusionConfig.default()
        config.scheduler.two_stage_enabled = True
        config.budgets.total_candidates = 100_000
        config.budgets.fusion_comparisons = 5
        controller = AdmissionController(self.docs, config)
        decisions = [
            controller.admit(query, config.scheduler.batch_size)
            for query in self.queries
        ]
        rejected = [d for d in decisions if not d.admitted]
        self.assertTrue(rejected)
        self.assertTrue(
            all(
                d.rejection_code == ErrorCode.E_FUSION_BUDGET
                for d in rejected
            )
        )

    def test_shrink_arbitration_sacrifices_precision_before_reject(self):
        config = FusionConfig.default()
        config.budgets.total_candidates = 120
        config.budgets.fusion_comparisons = 100_000
        config.scheduler.expansion_step = 10
        config.scheduler.min_channel_floor = 1
        controller = AdmissionController(self.docs, config)
        for query in self.queries:
            controller.admit(query, config.scheduler.batch_size)
        self.assertTrue(controller.arbitrations)
        arb = controller.arbitrations[0]
        self.assertEqual(arb.sacrificed_metric, "precision")
        self.assertIn("shrunk_caps", arb.detail)


class MemoryTest(unittest.TestCase):
    def test_eviction_frees_bytes_and_preserves_fused_trace(self):
        result_a = build_result("q_a", 40)
        result_b = build_result("q_b", 40)
        size_a = result_resident_bytes(result_a)
        size_b = result_resident_bytes(result_b)
        manager = ResidentMemoryManager(limit_bytes=max(size_a, size_b) + 256)
        manager.register(result_a)
        events = manager.register(result_b)
        self.assertTrue(events)
        event: EvictionEvent = events[0]
        self.assertEqual(event.evicted_query_ids, ["q_a"])
        self.assertGreater(event.released_bytes, 0)
        compact = event.compacted_results["q_a"]
        self.assertEqual(event.retained_bytes, size_b)
        # Bulky arrays dropped, but fused candidates and counts remain.
        self.assertEqual(compact.channel_candidates, {})
        self.assertEqual(compact.merged, [])
        self.assertEqual(len(compact.fused), 40)
        self.assertEqual(compact.cost_recall, 40)
        self.assertLess(result_total_bytes(compact), result_total_bytes(result_a))

    def test_oversized_shard_raises_resource_limit(self):
        result = build_result("q_big", 20)
        manager = ResidentMemoryManager(limit_bytes=1)
        from recall_fusion.errors import PipelineError

        with self.assertRaises(PipelineError) as ctx:
            manager.register(result)
        self.assertEqual(ctx.exception.code, ErrorCode.E_RESOURCE_LIMIT)


if __name__ == "__main__":
    unittest.main()
