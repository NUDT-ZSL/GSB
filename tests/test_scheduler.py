import json
import tempfile
import unittest
from pathlib import Path

from recall_fusion.config import FusionConfig
from recall_fusion.errors import ErrorCode
from recall_fusion.pipeline import FusionPipeline
from recall_fusion.sample_data import generate_documents, generate_queries


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.docs = generate_documents(100, seed=2026)
        self.queries = generate_queries(20, seed=2026)

    def _run(self, in_process: bool, overrides=None):
        config = FusionConfig.default(overrides)
        pipeline = FusionPipeline(self.docs, config, in_process=in_process)
        report = pipeline.run(self.queries)
        return pipeline, report

    def test_pool_and_in_process_results_are_identical(self):
        pipe_in, report_in = self._run(in_process=True)
        pipe_pool, report_pool = self._run(in_process=False)
        self.assertEqual(
            sorted(pipe_in.scheduler.results),
            sorted(pipe_pool.scheduler.results),
        )
        for query_id in pipe_in.scheduler.results:
            a = pipe_in.scheduler.results[query_id]
            b = pipe_pool.scheduler.results[query_id]
            self.assertEqual(
                [c.doc_id for c in a.fused], [c.doc_id for c in b.fused]
            )
            self.assertEqual(
                [c.final_score for c in a.fused],
                [c.final_score for c in b.fused],
            )
        self.assertEqual(report_in.batches, report_pool.batches)

    def test_deterministic_chunk_boundaries_and_execution_order(self):
        overrides = {"scheduler": {"batch_size": 3, "workers": 2}}
        _, report = self._run(True, overrides)
        flat = [qid for batch in report.batches for qid in batch]
        self.assertEqual(flat, sorted(flat))
        self.assertTrue(all(len(batch) <= 3 for batch in report.batches))
        self.assertEqual(report.execution_order, sorted(report.execution_order))
        _, report2 = self._run(False, overrides)
        self.assertEqual(report.batches, report2.batches)

    def test_worker_error_is_structured_and_isolated(self):
        config = FusionConfig.default()
        from recall_fusion.worker import process_batch

        good = sorted(self.queries[:6], key=lambda q: q.query_id)
        bad_config = FusionConfig.default()
        bad_config.channels["vector"].normalization = "illegal_norm"
        assignments = [
            {
                "query": query.__dict__.copy(),
                "caps": {name: 10 for name in
                         ("inverted", "prefix", "category", "vector")},
            }
            for query in good
        ]
        payload = process_batch(
            self.docs, bad_config.to_dict(), batch_id=7, assignments=assignments
        )
        self.assertEqual(len(payload["errors"]), len(good))
        error = payload["errors"][0]
        self.assertEqual(error["code"], ErrorCode.E_NORM_INVALID)
        self.assertEqual(error["stage"], "normalization")
        self.assertEqual(error["batch_id"], 7)

        # A healthy batch in the same pool run still completes unaffected.
        healthy = process_batch(
            self.docs, config.to_dict(), batch_id=8, assignments=assignments
        )
        self.assertEqual(healthy["errors"], [])
        self.assertEqual(
            sorted(row["query_id"] for row in healthy["results"]),
            [q.query_id for q in good],
        )

    def test_rejected_queries_never_appear_completed(self):
        overrides = {
            "budgets": {"total_candidates": 25, "fusion_comparisons": 100_000}
        }
        _, report = self._run(True, overrides)
        self.assertTrue(report.rejected_query_ids)
        self.assertEqual(
            set(report.rejected_query_ids) & set(report.completed_query_ids),
            set(),
        )

    def test_eviction_run_matches_no_eviction_run_results(self):
        tight = {
            "scheduler": {"batch_size": 4},
            "budgets": {
                "total_candidates": 100_000,
                "fusion_comparisons": 100_000,
                "resident_memory_bytes": 30_000,
            }
        }
        loose = {
            "budgets": {
                "total_candidates": 100_000,
                "fusion_comparisons": 100_000,
                "resident_memory_bytes": 10_000_000,
            }
        }
        pipe_tight, report_tight = self._run(True, tight)
        pipe_loose, _ = self._run(True, loose)
        self.assertTrue(report_tight.eviction_events)
        for query_id in pipe_tight.scheduler.results:
            self.assertEqual(
                [c.final_score for c in pipe_tight.scheduler.results[query_id].fused],
                [c.final_score for c in pipe_loose.scheduler.results[query_id].fused],
            )

    def test_conflict_arbitration_is_recorded_and_deterministic(self):
        overrides = {
            "scheduler": {"workers": 4, "batch_size": 8, "two_stage_enabled": True},
            "budgets": {
                "total_candidates": 100_000,
                "fusion_comparisons": 200,
                "resident_memory_bytes": 2048,
            },
        }
        _, report = self._run(True, overrides)
        self.assertTrue(report.arbitrations)
        triggers = {arb["trigger"] for arb in report.arbitrations}
        self.assertTrue(triggers & {"memory_parallelism_conflict",
                                    "two_stage_fusion_budget_conflict",
                                    "batch_memory_conflict"})
        _, report2 = self._run(True, overrides)
        self.assertEqual(report.arbitrations, report2.arbitrations)

    def test_report_is_json_serializable_and_saved(self):
        pipeline, report = self._run(True)
        with tempfile.TemporaryDirectory() as tmp:
            paths = pipeline.save_artifacts(tmp, report)
            data = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["completed"], report.stats["completed"])
            self.assertEqual(
                data["completed_query_ids"], report.completed_query_ids
            )
            self.assertTrue(Path(paths["results"]).exists())

    def test_illegal_merge_policy_surfaces_as_structured_worker_error(self):
        config = FusionConfig.default()
        config.scheduler.merge_policy = "bad_policy"
        pipeline = FusionPipeline(self.docs, config, in_process=True)
        report = pipeline.run(self.queries)
        self.assertTrue(report.worker_errors)
        error = report.worker_errors[0]
        self.assertEqual(error["code"], ErrorCode.E_MERGE_POLICY)
        self.assertEqual(error["stage"], "merge")
        self.assertEqual(error["field"], "merge_policy")
        self.assertIsNotNone(error["batch_id"])
        self.assertEqual(report.completed_query_ids, [])


if __name__ == "__main__":
    unittest.main()
