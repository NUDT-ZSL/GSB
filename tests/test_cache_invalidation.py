from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ragdiag.cache import ContentAddressedCache
from ragdiag.runtime import run_workbench
from ragdiag.sample_generator import generate


class CacheInvalidationTests(unittest.TestCase):
    def test_cache_hits_snapshot_replacement_and_dependency_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            generate(data)
            cache_dir = root / "cache"
            baseline = run_workbench(
                data / "tool_results.json",
                data / "requests.jsonl",
                data / "schedule.json",
                data / "evidence_corpus.jsonl",
                cache_dir,
                output_json=root / "baseline.json",
                output_html=root / "baseline.html",
            )
            self.assertEqual(
                baseline["summary"]["business_codes"],
                {
                    "E_CONCURRENCY_BUDGET": 1,
                    "E_MEMORY_BUDGET": 1,
                    "E_OK": 6,
                    "E_TIME_BUDGET": 1,
                },
            )
            repeated = run_workbench(
                data / "tool_results.json",
                data / "requests.jsonl",
                data / "schedule.json",
                data / "evidence_corpus.jsonl",
                cache_dir,
            )
            self.assertEqual(
                repeated["summary"]["cache_hit_requests"], 6
            )
            self.assertEqual(
                repeated["invalidation"]["counts"]["recompute_nodes"], 0
            )
            self.assertTrue(
                repeated["invalidation"]["coverage"][
                    "affected_requests_covered"
                ]
            )
            updated = run_workbench(
                data / "tool_results.next.json",
                data / "requests.jsonl",
                data / "schedule.json",
                data / "evidence_corpus.jsonl",
                cache_dir,
                output_json=root / "invalidation.json",
                output_html=root / "invalidation.html",
                baseline_repository_path=data / "tool_results.json",
            )
            invalidation = updated["invalidation"]
            self.assertEqual(
                invalidation["affected_requests"],
                [
                    "conv-001@1",
                    "conv-001@2",
                    "conv-005@1",
                    "conv-007@1",
                ],
            )
            self.assertEqual(
                invalidation["affected_stages"],
                ["merge", "retrieve", "tool_read"],
            )
            self.assertTrue(invalidation["coverage"]["disjoint"])
            self.assertTrue(
                invalidation["coverage"]["affected_requests_covered"]
            )
            self.assertEqual(
                updated["summary"]["cache_hit_requests"], 3
            )
            self.assertTrue((root / "invalidation.html").exists())
            cache = ContentAddressedCache(cache_dir)
            snapshots = cache.list_snapshots()
            self.assertGreaterEqual(len(snapshots), 3)
            previous_id = updated["snapshot"]["previous_snapshot_id"]
            cache.activate_snapshot(previous_id)
            self.assertEqual(
                cache.active_snapshot()["snapshot_id"], previous_id
            )

    def test_schedule_change_only_invalidates_merge_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            generate(data)
            cache_dir = root / "cache"
            run_workbench(
                data / "tool_results.json",
                data / "requests.jsonl",
                data / "schedule.json",
                data / "evidence_corpus.jsonl",
                cache_dir,
            )
            changed = run_workbench(
                data / "tool_results.json",
                data / "requests.jsonl",
                data / "schedule.tuned.json",
                data / "evidence_corpus.jsonl",
                cache_dir,
                baseline_repository_path=data / "tool_results.json",
                baseline_schedule_path=data / "schedule.json",
            )
            invalidation = changed["invalidation"]
            self.assertEqual(
                invalidation["affected_requests"],
                [
                    "conv-001@1",
                    "conv-001@2",
                    "conv-002@1",
                    "conv-003@1",
                    "conv-004@1",
                    "conv-005@1",
                    "conv-006@1",
                    "conv-007@1",
                    "conv-008@1",
                ],
            )
            self.assertEqual(
                invalidation["affected_stages"], ["merge", "retrieve"]
            )
            self.assertTrue(
                invalidation["coverage"]["affected_requests_covered"]
            )


if __name__ == "__main__":
    unittest.main()

