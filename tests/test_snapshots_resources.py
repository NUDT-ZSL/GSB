from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from corpus_dedup_workbench.errors import (
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    WorkbenchError,
)
from corpus_dedup_workbench.snapshots import SnapshotManager
from tests.helpers import make_service


class SnapshotTests(unittest.TestCase):
    def test_snapshot_replace_is_whole_object_with_checksum(self) -> None:
        manager = SnapshotManager()
        first = manager.publish("stats", {"a": 1, "nested": {"x": 2}})
        self.assertEqual(first.version, 1)
        second = manager.publish("stats", {"b": 3})
        self.assertEqual(second.version, 2)
        current = manager.get("stats")
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["payload"], {"b": 3})
        self.assertNotIn("a", current["payload"])
        self.assertEqual(len(current["checksum"]), 64)

    def test_corrupt_payload_and_checksum_mismatch_are_rejected(self) -> None:
        manager = SnapshotManager()
        manager.publish("stats", {"a": 1})
        manager._tamper_for_test("stats", {"a": 999})
        with self.assertRaises(WorkbenchError) as caught:
            manager.get("stats")
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)

        fresh = SnapshotManager()
        with self.assertRaises(WorkbenchError) as caught:
            fresh.publish("stats", {"a": 1}, expected_checksum="bad")
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)

    def test_rollback_and_same_version_overwrite_are_rejected(self) -> None:
        manager = SnapshotManager()
        manager.publish("stats", {"a": 1}, version=3)
        for version in (2, 3):
            with self.assertRaises(WorkbenchError) as caught:
                manager.publish("stats", {"a": version}, version=version)
            self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_service_publishes_two_monotonic_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            before = service.status()["snapshots"]
            service.refresh()
            after = service.status()["snapshots"]
            self.assertEqual(set(after), {"removed_list", "shard_statistics"})
            for kind in after:
                self.assertGreater(after[kind]["version"], before[kind]["version"])
                self.assertEqual(len(after[kind]["checksum"]), 64)

    def test_mode_switch_changes_snapshot_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            before = service.status()["snapshots"]
            service.reconfigure({"validation_mode": "digest_summary"})
            after = service.status()["snapshots"]
            for kind in ("removed_list", "shard_statistics"):
                self.assertGreater(after[kind]["version"], before[kind]["version"])
                self.assertNotEqual(
                    after[kind]["checksum"], before[kind]["checksum"]
                )


class ResourceTests(unittest.TestCase):
    def test_document_limit_returns_resource_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(WorkbenchError) as caught:
                make_service(directory, max_docs_per_batch=10)
            self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)

    def test_candidate_limit_error_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(WorkbenchError) as caught:
                make_service(
                    directory,
                    max_candidate_comparisons=1,
                    resource_policy="error",
                )
            self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(
                caught.exception.details["max_candidate_comparisons"], 1
            )

    def test_candidate_limit_is_global_across_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(WorkbenchError) as caught:
                make_service(
                    directory,
                    max_candidate_comparisons=30,
                    resource_policy="error",
                )
            self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(
                caught.exception.details["effective_batch_remaining"], 12
            )

            service = make_service(
                Path(directory) / "degrade",
                max_candidate_comparisons=30,
                resource_policy="degrade",
            )
            batch = service.batch()
            self.assertEqual(batch["totals"]["candidate_pair_count"], 30)
            self.assertEqual(
                batch["shard_stats"]["shard-00"]["candidate_pair_count"], 18
            )
            self.assertEqual(
                batch["shard_stats"]["shard-01"]["candidate_pair_count"], 12
            )
            self.assertEqual(
                batch["shard_stats"]["shard-02"]["candidate_pair_count"], 0
            )

    def test_candidate_limit_degradation_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(
                directory,
                max_candidate_comparisons=1,
                resource_policy="degrade",
            )
            batch = service.batch()
            self.assertTrue(batch["totals"]["degraded"])
            self.assertEqual(batch["totals"]["candidate_pair_count"], 1)
            for shard_id, stats in batch["shard_stats"].items():
                self.assertTrue(stats["degraded"])
                self.assertEqual(
                    stats["degradation"]["category"],
                    "candidate_comparisons_truncated",
                )
                if shard_id == "shard-00":
                    self.assertEqual(stats["candidate_pair_count"], 1)
                else:
                    self.assertEqual(stats["candidate_pair_count"], 0)
            status = service.status()
            self.assertTrue(status["degradation"]["degraded"])
            self.assertIn(
                "candidate_comparisons_truncated",
                status["degradation"]["categories"],
            )

    def test_cache_limit_degrades_with_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory, max_cache_entries=1)
            status = service.status()
            self.assertGreaterEqual(status["cache"]["evictions"], 1)
            self.assertIn("cache_lru_eviction", status["degradation"]["categories"])


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_queries_during_snapshot_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(
                directory,
                latency_budget_ms=5000.0,
            )
            doc_ids = [f"doc-{index:06d}" for index in range(0, 24, 4)]
            errors: list[BaseException] = []

            def query_loop(doc_id: str) -> None:
                try:
                    for _ in range(4):
                        result = service.query(doc_id)
                        if not result["consistency"]["checked"]:
                            raise AssertionError("consistency not checked")
                except BaseException as exc:  # noqa: BLE001 - propagated below
                    errors.append(exc)

            def switch_modes() -> None:
                try:
                    for mode in (
                        "digest_summary",
                        "full",
                        "digest_summary",
                        "full",
                    ):
                        service.reconfigure({"validation_mode": mode})
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(query_loop, doc_id) for doc_id in doc_ids
                ]
                futures.append(executor.submit(switch_modes))
                for future in futures:
                    future.result()
            self.assertEqual(errors, [])
            snapshots = service.status()["snapshots"]
            self.assertGreaterEqual(snapshots["removed_list"]["version"], 5)
            self.assertGreaterEqual(
                snapshots["shard_statistics"]["version"], 5
            )


if __name__ == "__main__":
    unittest.main()

