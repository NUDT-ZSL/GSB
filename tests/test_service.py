from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from corpus_dedup_workbench.errors import (
    E_DOC_NOT_FOUND,
    E_INCONSISTENT,
    E_LATENCY_BUDGET,
    WorkbenchError,
)
from tests.helpers import make_service


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = make_service(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_query_and_batch_are_consistent_for_every_doc(self) -> None:
        batch = self.service.batch()
        report = self.service.validate_all_consistency()
        self.assertTrue(report["all_consistent"])
        self.assertEqual(report["checked_docs"], batch["totals"]["doc_count"])
        for doc_id in sorted(batch["docs"]):
            query = self.service.query(doc_id)
            self.assertEqual(
                query["block_signatures"],
                batch["docs"][doc_id]["block_signatures"],
            )
            self.assertEqual(
                query["candidate_trace"],
                batch["docs"][doc_id]["candidate_trace"],
            )
            self.assertEqual(
                query["similar_decision"],
                batch["docs"][doc_id]["similar_decision"],
            )
            self.assertEqual(
                query["removal_reason"],
                batch["docs"][doc_id]["removal_reason"],
            )
            self.assertTrue(query["consistency"]["checked"])
            self.assertTrue(query["consistency"]["candidate_set_match"])
            self.assertTrue(query["consistency"]["bucket_match"])

    def test_unknown_doc_id_returns_business_error(self) -> None:
        with self.assertRaises(WorkbenchError) as caught:
            self.service.query("missing-doc")
        self.assertEqual(caught.exception.code, E_DOC_NOT_FOUND)

    def test_latency_budget_uses_actual_timing_and_preserves_stages(self) -> None:
        with self.assertRaises(WorkbenchError) as caught:
            self.service.query("doc-000000", latency_budget_ms=1e-9)
        error = caught.exception
        self.assertEqual(error.code, E_LATENCY_BUDGET)
        self.assertTrue(error.completed_stages)
        self.assertEqual(error.details["candidate_comparisons_skipped"], 0)
        self.assertGreater(error.details["actual_elapsed_ms"], 0)

    def test_latency_budget_cannot_be_amplified_above_config(self) -> None:
        query = self.service.query("doc-000000", latency_budget_ms=999999)
        self.assertEqual(
            query["latency"]["budget_ms"],
            self.service.config.latency_budget_ms,
        )
        self.assertEqual(
            query["latency"]["budget_ceiling_ms"],
            self.service.config.latency_budget_ms,
        )

    def test_inconsistency_detection_reports_first_diff(self) -> None:
        self.service.inject_query_fault(
            "doc-000000", "block_signatures", "deadbeefdeadbeef"
        )
        try:
            with self.assertRaises(WorkbenchError) as caught:
                self.service.query("doc-000000")
        finally:
            self.service.clear_query_faults()
        error = caught.exception
        self.assertEqual(error.code, E_INCONSISTENT)
        self.assertEqual(error.stage, "block_signatures")
        details = error.details
        self.assertEqual(details["first_diff_doc_id"], "doc-000000")
        self.assertEqual(details["precision"], "1e-12")
        self.assertIn("query_value", details)
        self.assertIn("batch_value", details)

    def test_cache_reuse_and_configuration_invalidation(self) -> None:
        first = self.service.batch()
        initial_keys = len(first["cache_report"]["recomputed_keys"])
        self.assertEqual(initial_keys, 4)
        refreshed = self.service.refresh()
        self.assertEqual(refreshed["cache_report"]["recomputed_docs"], [])
        self.assertEqual(len(refreshed["cache_report"]["reused_docs"]), 72)
        transition = self.service.reconfigure(
            {"similarity_threshold": "0.80"}
        )
        self.assertTrue(transition["changed"])
        report = transition["transition"]
        self.assertEqual(
            report["changed_fields"], ["similarity_threshold"]
        )
        self.assertTrue(report["affected_docs"])
        self.assertEqual(len(report["affected_cache_entries"]), 4)
        self.assertEqual(len(report["invalidated_entries"]), 4)
        self.assertEqual(report["reused_docs"], [])
        self.assertEqual(len(report["recomputed_docs"]), 72)
        after = self.service.batch()
        self.assertEqual(
            sum(stats["removed_count"] for stats in after["shard_stats"].values()),
            after["totals"]["removed_count"],
        )

    def test_cache_keys_change_with_required_fields(self) -> None:
        content_hash = "fixed-content-hash"
        base = self.service._shard_cache_key(
            "shard-00", content_hash, self.service.config
        )
        variants = {
            base,
            self.service._shard_cache_key(
                "shard-00",
                content_hash,
                self.service.config.replaced({"signature_length": 13}),
            ),
            self.service._shard_cache_key(
                "shard-00",
                content_hash,
                self.service.config.replaced({"similarity_threshold": "0.8"}),
            ),
            self.service._shard_cache_key(
                "shard-00",
                content_hash,
                self.service.config.replaced({"candidate_window": 3}),
            ),
            self.service._shard_cache_key(
                "shard-00",
                content_hash,
                self.service.config.replaced(
                    {"validation_mode": "digest_summary"}
                ),
            ),
        }
        self.assertEqual(len(variants), 5)

    def test_digest_mode_queries_remain_consistent(self) -> None:
        transition = self.service.reconfigure(
            {"validation_mode": "digest_summary"}
        )
        self.assertTrue(transition["changed"])
        report = self.service.validate_all_consistency()
        self.assertTrue(report["all_consistent"])
        batch = self.service.batch()
        self.assertGreater(
            batch["totals"]["skipped_candidate_count"], 0
        )


if __name__ == "__main__":
    unittest.main()

