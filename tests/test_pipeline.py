from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from corpus_dedup_workbench.pipeline import process_shard
from tests.helpers import make_service, sample_config


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_window_is_enforced(self) -> None:
        service = make_service(self.root, candidate_window=1)
        result = service.batch()
        candidates = result["candidate_pairs"]
        self.assertTrue(candidates)
        self.assertTrue(
            all(candidate["window_distance"] <= 1 for candidate in candidates)
        )
        expected = sum(max(0, bucket["size"] - 1) for bucket in result["buckets"])
        self.assertEqual(len(candidates), expected)
        for candidate in candidates:
            self.assertEqual(candidate["a_doc"][:4], candidate["b_doc"][:4])
            self.assertIn(candidate["bucket_key"], {
                bucket["bucket_key"] for bucket in result["buckets"]
            })

    def test_zero_window_creates_empty_candidate_records(self) -> None:
        service = make_service(self.root, candidate_window=0)
        result = service.batch()
        self.assertEqual(result["candidate_pairs"], [])
        self.assertTrue(result["buckets"])
        self.assertTrue(
            all(bucket["empty_candidates"] for bucket in result["buckets"])
        )
        self.assertEqual(result["totals"]["candidate_pair_count"], 0)
        self.assertEqual(result["totals"]["removed_count"], 0)

    def test_bucket_and_candidate_counts_are_reconcilable(self) -> None:
        service = make_service(self.root)
        result = service.batch()
        for bucket in result["buckets"]:
            self.assertEqual(
                bucket["candidate_count"],
                len(bucket["candidate_ids"]),
            )
            self.assertEqual(
                bucket["empty_candidates"],
                bucket["candidate_count"] == 0,
            )
        self.assertEqual(
            result["totals"]["candidate_pair_count"],
            sum(bucket["candidate_count"] for bucket in result["buckets"]),
        )
        self.assertEqual(
            result["totals"]["bucket_count"], len(result["buckets"])
        )

    def test_candidate_trace_contains_hits_similarity_and_threshold(self) -> None:
        service = make_service(self.root)
        result = service.batch()
        candidate = result["candidate_pairs"][0]
        self.assertIn("hit_positions", candidate)
        self.assertIn("similarity", candidate)
        self.assertIn("threshold_trace", candidate)
        self.assertIn(candidate["summary_relation"], {"same", "conflict"})
        self.assertEqual(candidate["summary_relation"], "same")
        self.assertIsInstance(candidate["threshold_trace"]["decision"], bool)

    def test_quality_then_doc_id_selects_representative(self) -> None:
        service = make_service(self.root)
        result = service.batch()
        removed = {item["doc_id"]: item for item in result["removed"]}
        self.assertTrue(removed)
        for reason in removed.values():
            doc = result["docs"][reason["doc_id"]]
            representative = result["docs"][reason["neighbor_doc_id"]]
            self.assertFalse(representative["removed"])
            self.assertGreaterEqual(
                representative["quality_score"], doc["quality_score"]
            )
            self.assertEqual(reason["candidate_id"], reason["candidate_id"])
            self.assertTrue(reason["hit_positions"])
            self.assertTrue(reason["threshold_trace"]["decision"])
            self.assertEqual(reason["summary_key"], doc["summary"]["key"])

    def test_equal_quality_uses_doc_id_tiebreak(self) -> None:
        text = " ".join(f"tie{index:03d}" for index in range(192))
        docs = [
            {
                "shard_id": "shard-tie",
                "doc_id": doc_id,
                "text": text,
                "lang": "en",
                "source": "unit-test",
                "token_estimate": 192,
                "quality_score": 50.0,
            }
            for doc_id in ("doc-tie-b", "doc-tie-a")
        ]
        result = process_shard("shard-tie", docs, sample_config())
        self.assertEqual(result["kept"], ["doc-tie-a"])
        self.assertEqual(result["removed"][0]["doc_id"], "doc-tie-b")
        self.assertEqual(
            result["removed"][0]["neighbor_doc_id"], "doc-tie-a"
        )

    def test_mode_switch_reports_removed_and_hit_position_differences(self) -> None:
        service = make_service(self.root)
        result = service.compare_modes()
        only_full = result["removed_set_difference"]["only_full_mode_removed"]
        self.assertTrue(only_full)
        self.assertTrue(result["hit_position_differences"])
        self.assertGreater(
            result["attribution_summary"]["summary_conflict_omission"], 0
        )
        self.assertGreater(
            result["attribution_summary"]["hit_block_reduction"], 0
        )
        explanations = {
            item["detail"]
            for items in result["attribution"].values()
            for item in items
        }
        self.assertTrue(any("摘要冲突" in text for text in explanations))
        self.assertTrue(any("命中块减少" in text for text in explanations))
        digest_candidates = result["digest_mode"]["candidate_pair_count"]
        full_candidates = result["full_mode"]["candidate_pair_count"]
        self.assertEqual(digest_candidates, full_candidates)


if __name__ == "__main__":
    unittest.main()

