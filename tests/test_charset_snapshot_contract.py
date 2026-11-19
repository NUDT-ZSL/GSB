import os
import tempfile
import unittest

from ocr_pipeline.cache import RecognitionCache
from ocr_pipeline.engine import OCREngine
from ocr_pipeline.errors import StageBusyError
from ocr_pipeline.recognition import RecognitionParams
from ocr_pipeline.sample import generate_page
from ocr_pipeline.snapshots import SnapshotStore
from ocr_pipeline.templates import build_library, subset_library


class CharsetShrinkTests(unittest.TestCase):
    def test_shrink_changes_checksum_and_forces_recompute(self):
        library = build_library("cs-v1", 1)
        page = generate_page("p-shrink", 555, library)
        cache = RecognitionCache()
        engine = OCREngine(library, RecognitionParams(), cache)
        _result, first = engine.process_page(
            page, chunk_size=2, latency_budget_ms=50.0,
            memory_ceiling_bytes=200_000,
        )
        self.assertEqual(len(cache.all_entries()), len(first["recompute_lines"]))

        shrunk = subset_library(library, "ABCDE012", new_version=3)
        self.assertNotEqual(shrunk.checksum, library.checksum)
        removed = cache.invalidate(
            library_versions={library.library_version},
            charset_alphabets={library.library_version: library.alphabet},
        )
        self.assertTrue(removed)
        shrunk_engine = OCREngine(shrunk, RecognitionParams(), cache)
        _result, report = shrunk_engine.process_page(
            page, chunk_size=2, latency_budget_ms=50.0,
            memory_ceiling_bytes=200_000,
        )
        self.assertEqual(report["reused_lines"], [])
        self.assertTrue(report["recompute_lines"])


class SnapshotCommitContractTests(unittest.TestCase):
    def test_single_effective_commit_non_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(directory)
            store._lock.acquire()
            with self.assertRaises(StageBusyError) as caught:
                store.commit("page_results", 1, {"a": 1})
            self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
            store._lock.release()
            envelope = store.commit("page_results", 1, {"a": 1})
            self.assertTrue(os.path.exists(store.path_for("page_results")))
            self.assertEqual(envelope["version"], 1)


if __name__ == "__main__":
    unittest.main()
