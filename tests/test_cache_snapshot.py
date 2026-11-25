"""要求 6/7：内容寻址失效传播、版本化快照与并发提交控制。"""

from __future__ import annotations

import json
from pathlib import Path

from sr_pipeline.cache import (
    SnapshotStore,
    build_module_keys,
    content_checksum,
    plan_invalidation,
)
from sr_pipeline.errors import BusinessError, ErrorCode
from sr_pipeline.pipeline import execute_pipeline

from ._base import SampleCase


class CacheSnapshotTest(SampleCase):
    def _db(self) -> Path:
        return self.sample_dir / "cache.sqlite"

    def test_first_run_recompute_second_run_reuse(self) -> None:
        first = execute_pipeline(self.sample_dir, self._db())
        self.assertEqual(
            len(first.invalidation["recompute_queries"]),
            len(self.queries),
        )
        second = execute_pipeline(self.sample_dir, self._db())
        self.assertEqual(second.invalidation["changed_modules"], [])
        self.assertEqual(
            len(second.invalidation["reuse_queries"]), len(self.queries)
        )
        self.assertEqual(second.invalidation["recompute_queries"], [])
        self.assertTrue(
            all(item["cache_hit"] for item in second.self_check["reuse_manifest"])
        )

    def test_term_weight_change_invalidates_downstream(self) -> None:
        execute_pipeline(self.sample_dir, self._db())
        weights_path = self.sample_dir / "term_weights.json"
        weights = json.loads(weights_path.read_text(encoding="utf-8"))
        weights["0"] = round(float(weights["0"]) + 0.123, 3)
        weights_path.write_text(json.dumps(weights, sort_keys=True), encoding="utf-8")
        result = execute_pipeline(self.sample_dir, self._db())
        changed = result.invalidation["changed_modules"]
        # 权重变化必须沿依赖链传播到 index/recall/fusion
        for module in ("term_weights", "index", "recall", "fusion"):
            self.assertIn(module, changed)
        self.assertEqual(
            len(result.invalidation["recompute_queries"]), len(self.queries)
        )

    def test_doc_revision_changes_docs_key_only_upstream(self) -> None:
        old = build_module_keys("d", "w", "r", "f", "p")
        new = build_module_keys("d2", "w", "r", "f", "p")
        plan = plan_invalidation(
            old, new, ["q1", "q2"], {"recall": {"q1"}}
        )
        self.assertIn("docs", plan.changed_modules)
        self.assertIn("index", plan.changed_modules)
        self.assertNotIn("route_config", plan.changed_modules)
        self.assertEqual(plan.affected_queries, {"q1", "q2"})

    def test_snapshot_monotonic_versions_and_replace(self) -> None:
        with SnapshotStore(self._db()) as store:
            first = store.commit_snapshot("stats", {"v": 1})
            second = store.commit_snapshot("stats", {"v": 2})
            third = store.commit_snapshot("stats", {"v": 2})  # 幂等
            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            self.assertFalse(third["replaced"])
            self.assertEqual(third["version"], 2)
            history = store.snapshot_history("stats")
            self.assertEqual([row["version"] for row in history], [1, 2])

    def test_optimistic_concurrency_conflict(self) -> None:
        with SnapshotStore(self._db()) as store:
            store.commit_snapshot("stats", {"v": 1})
            store.commit_snapshot("stats", {"v": 2})
            with self.assertRaises(BusinessError) as caught:
                store.commit_snapshot("stats", {"v": 3}, expected_version=1)
            self.assertEqual(
                caught.exception.code, ErrorCode.VERSION_CONFLICT
            )

    def test_checksum_corruption_detected(self) -> None:
        with SnapshotStore(self._db()) as store:
            store.put_entry("fusion", "k1", {"score": 1.0})
            store._conn.execute(
                "UPDATE cache_entries SET checksum='bad' WHERE module='fusion'"
            )
            with self.assertRaises(BusinessError) as caught:
                store.get_entry("fusion", "k1")
            self.assertEqual(
                caught.exception.code, ErrorCode.CHECKSUM_MISMATCH
            )

    def test_commit_seq_single_serial_chain(self) -> None:
        with SnapshotStore(self._db()) as store:
            store.commit_snapshot("a", {"x": 1})
            store.commit_snapshot("b", {"x": 1})
            store.commit_snapshot("a", {"x": 2})
            seq_a = [
                row["committed_seq"]
                for row in store.snapshot_history("a")
            ]
            self.assertEqual(seq_a, [1, 3])  # 全局单调，跨快照类型共享
            self.assertEqual(content_checksum({"x": 1}) == content_checksum({"x": 1}), True)
