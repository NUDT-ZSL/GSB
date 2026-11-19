import os
import tempfile
import unittest

from ocr_pipeline.chunking import ResourceObservation, plan_chunk_size
from ocr_pipeline.engine import OCREngine
from ocr_pipeline.errors import (
    LatencyBudgetError,
    SnapshotCorruptError,
    StageBusyError,
)
from ocr_pipeline.recognition import RecognitionParams
from ocr_pipeline.sample import generate_page
from ocr_pipeline.snapshots import SnapshotStore, wrap_snapshot
from ocr_pipeline.templates import build_library
from ocr_pipeline.util import read_json, write_json


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.library = build_library("cs-v1", 1)
        self.engine = OCREngine(self.library, RecognitionParams())
        self.page = generate_page("p-lat", 99, self.library)

    def test_query_within_budget_returns_full_detail(self):
        result = self.engine.query_line(self.page, 1, latency_budget_ms=10_000.0)
        self.assertEqual(result["line_no"], 1)
        self.assertEqual(len(result["box"]), 4)
        self.assertTrue(result["candidates"])
        self.assertLessEqual(result["timing"]["elapsed_ms"], 10_000.0)
        self.assertIn("recognition", result["timing"]["completed_stages"])

    def test_zero_budget_always_overruns_with_real_timing(self):
        with self.assertRaises(LatencyBudgetError) as caught:
            self.engine.query_line(self.page, 0, latency_budget_ms=0.0)
        context = caught.exception.context
        self.assertEqual(caught.exception.code, "E_LATENCY_BUDGET")
        self.assertGreaterEqual(context["timing"]["elapsed_ms"], 0.0)
        self.assertIn("ready_lines", context["timing"])
        self.assertIn("completed_stages", context["timing"])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_versioned_commit_and_reload(self):
        envelope = self.store.commit("recognition_params", 1, {"a": 1})
        loaded, changed = self.store.reload_if_changed("recognition_params", None)
        self.assertTrue(changed)
        self.assertEqual(loaded.version, 1)
        self.assertEqual(envelope["checksum"], loaded.checksum)
        _again, changed_second = self.store.reload_if_changed(
            "recognition_params", loaded
        )
        self.assertFalse(changed_second)

    def test_monotonic_version_enforced(self):
        self.store.commit("page_results", 2, {"x": 1})
        with self.assertRaises(SnapshotCorruptError):
            self.store.commit("page_results", 2, {"x": 2})

    def test_corrupt_snapshot_rejected_entirely(self):
        self.store.commit("template_library", 1, {"ok": True})
        path = self.store.path_for("template_library")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"category":"template_library","version":3,"payload":')
        with self.assertRaises(SnapshotCorruptError) as caught:
            self.store.load("template_library")
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.context["category"], "template_library")

    def test_checksum_tampering_rejected(self):
        self.store.commit("recognition_params", 1, {"a": 1})
        path = self.store.path_for("recognition_params")
        payload = read_json(path)
        payload["checksum"] = "deadbeef"
        write_json(path, payload)
        with self.assertRaises(SnapshotCorruptError):
            self.store.load("recognition_params")

    def test_non_blocking_busy_lock(self):
        lock_store = SnapshotStore(os.path.join(self.temp.name, "nested"))
        lock_store._lock.acquire()
        with self.assertRaises(StageBusyError) as caught:
            lock_store.commit("page_results", 1, {"a": 1})
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")


class ArbitrationTests(unittest.TestCase):
    def _observation(self):
        return ResourceObservation(
            line_count=8,
            mean_line_pixels=120.0,
            mean_match_candidates=40.0,
            chunk_overhead_per_boundary=12.0,
            memory_bytes_per_line=960.0,
        )

    def test_plan_is_deterministic_and_prefers_small_chunk(self):
        observation = self._observation()
        first = plan_chunk_size(
            observation, latency_budget_ms=10_000.0,
            memory_ceiling_bytes=100_000,
        )
        second = plan_chunk_size(
            observation, latency_budget_ms=10_000.0,
            memory_ceiling_bytes=100_000,
        )
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.chunk_size, 1)
        self.assertEqual(first.affected_line_range, (0, 7))

    def test_memory_breach_never_sacrifices_correctness(self):
        plan = plan_chunk_size(
            self._observation(), latency_budget_ms=10_000.0,
            memory_ceiling_bytes=1,
        )
        self.assertIn("non-negotiable", plan.rationale)
        self.assertTrue(plan.chunk_size >= 1)


if __name__ == "__main__":
    unittest.main()
