import os
import shutil
import tempfile
import threading
import unittest

from ocrbench import consistency
from ocrbench.config import GRAPH_NODES, PipelineConfig, ResourceLimits
from ocrbench.errors import ErrorCode, OcrBenchError
from ocrbench.snapshot import TemplateSnapshotStore
from ocrbench.templates import build_library
from ocrbench.workbench import OcrWorkbench


class SnapshotVersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = TemplateSnapshotStore(os.path.join(self.tmp, "snap"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bump_replaces_whole_snapshot(self):
        v1 = self.store.replace(build_library(1))
        self.assertEqual(self.store.current_version(), 1)
        v2 = self.store.replace(build_library(2))
        self.assertEqual(self.store.current_version(), 2)
        self.assertNotEqual(v1.checksum, v2.checksum)
        loaded = self.store.load()
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.checksum, v2.checksum)

    def test_unchanged_version_is_reused_not_rewritten(self):
        first = self.store.replace(build_library(1))
        again = self.store.replace(build_library(1))
        self.assertEqual(first.checksum, again.checksum)

    def test_rollback_is_rejected(self):
        self.store.replace(build_library(2))
        with self.assertRaises(OcrBenchError) as ctx:
            self.store.replace(build_library(1))
        self.assertEqual(ctx.exception.code, ErrorCode.SNAPSHOT_ROLLBACK)
        self.assertEqual(self.store.current_version(), 2)

    def test_corrupt_snapshot_reports_version(self):
        self.store.replace(build_library(3))
        version = self.store.corrupt_for_diagnosis()
        with self.assertRaises(OcrBenchError) as ctx:
            self.store.load()
        self.assertEqual(ctx.exception.code, ErrorCode.CACHE_CORRUPT)
        self.assertEqual(ctx.exception.details["version"], version)


class ChunkConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())
        self.wb.create_dataset([("p1", 101), ("p2", 202)])
        self.wb.run_all()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_chunk_sizes_match_whole_page(self):
        for size in (1, 2, 3, 5, 100):
            outcome = self.wb.run_chunked("p1", size)
            self.assertTrue(outcome["identical"])
            whole_texts = [line["text"] for line in outcome["whole_records"]]
            chunk_texts = [line["text"] for line in outcome["records"]]
            self.assertEqual(whole_texts, chunk_texts)

    def test_chunk_mismatch_locates_first_bad_position(self):
        from ocrbench.engine import _first_text_mismatch

        whole = [{"text": "ABC"}, {"text": "HELLO"}]
        chunked = [{"text": "ABC"}, {"text": "HXLLO"}]
        self.assertEqual(_first_text_mismatch(whole, chunked), (1, 1))


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_commit_single_winner_and_busy_error(self):
        details = {
            "node": "recognize",
            "page_id": "p-conc",
            "pixel_hash": "a" * 64,
            "config_hash": "b" * 64,
            "upstream_hash": "c" * 64,
            "charset_version": 1,
            "dedup_threshold": 850,
            "line_box_summary": "d" * 16,
        }
        key = self.wb.cache.build_key("recognize", details)
        lock = self.wb.cache._acquire_stage(key)
        lock.acquire()
        outcomes = []

        def contender():
            try:
                self.wb.cache.commit("recognize", dict(details), {"v": 1})
            except OcrBenchError as error:
                outcomes.append(error.code)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join()
        lock.release()
        self.assertEqual(outcomes, [ErrorCode.STAGE_BUSY])

        # The losing commit must not have damaged or created the entry.
        committed = self.wb.cache.commit("recognize", dict(details), {"v": 1})
        self.assertEqual(committed.key, key)
        self.assertEqual(self.wb.cache.entry_count(), 1)


class ResourceAndSelfCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_line_limit_raises_resource_error(self):
        tight = PipelineConfig(
            resource_limits=ResourceLimits(max_lines_per_page=1)
        )
        wb = OcrWorkbench(os.path.join(self.tmp, "wb"), tight)
        wb.create_dataset([("p", 101)])
        with self.assertRaises(OcrBenchError) as ctx:
            wb.run_page("p")
        self.assertEqual(ctx.exception.code, ErrorCode.RESOURCE_LIMIT)

    def test_cache_capacity_limit(self):
        tight = PipelineConfig(
            resource_limits=ResourceLimits(max_cache_entries=1)
        )
        wb = OcrWorkbench(os.path.join(self.tmp, "wb"), tight)
        wb.create_dataset([("p", 101)])
        with self.assertRaises(OcrBenchError):
            wb.run_page("p")

    def test_self_checks_pass_on_healthy_run(self):
        wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())
        wb.create_dataset([("p", 101)])
        result = wb.run_page("p")
        for execution in result.executions:
            consistency.check_cache_key_and_hash(wb.cache, execution.key)
        consistency.check_reuse_recompute_partition(result, set(GRAPH_NODES))
        consistency.check_aggregate_recomputation(
            result, wb._require_page("p").lines, wb.config
        )

    def test_inconsistent_partition_detected(self):
        from ocrbench.engine import NodeExecution, PageRunResult

        wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())
        wb.create_dataset([("p", 101)])
        result = wb.run_page("p")
        # Forge an overlapping reuse/recompute execution set.
        forged = PageRunResult(
            page_id="p",
            executions=[
                NodeExecution("preprocess", "k1", "h1", True, {}),
                NodeExecution("preprocess", "k1", "h1", False, {}),
            ],
            payloads={},
            degraded=False,
            degradation_category="",
        )
        with self.assertRaises(OcrBenchError) as ctx:
            consistency.check_reuse_recompute_partition(forged, set())
        self.assertEqual(ctx.exception.code, ErrorCode.INCONSISTENT)


if __name__ == "__main__":
    unittest.main()
