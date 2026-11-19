import unittest

from ocr_pipeline.cache import RecognitionCache, build_cache_key
from ocr_pipeline.engine import OCREngine
from ocr_pipeline.recognition import RecognitionParams
from ocr_pipeline.sample import generate_page
from ocr_pipeline.templates import build_library


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.library = build_library("cs-v1", 1)
        self.params = RecognitionParams()
        self.page = generate_page("p-cache", 777, self.library)
        self.engine = OCREngine(self.library, self.params, RecognitionCache())

    def _process(self):
        return self.engine.process_page(
            self.page, chunk_size=2, latency_budget_ms=50.0,
            memory_ceiling_bytes=200_000,
        )

    def test_first_run_recomputes_second_run_reuses(self):
        _result, first = self._process()
        self.assertEqual(first["reused_lines"], [])
        self.assertNotEqual(first["recompute_lines"], [])
        _result, second = self._process()
        self.assertEqual(second["recompute_lines"], [])
        self.assertEqual(
            sorted(second["reused_lines"]),
            sorted(first["recompute_lines"]),
        )

    def test_cache_key_changes_with_each_dimension(self):
        common = dict(
            line_no=0,
            image_hash="img", boxes_summary="box", library_version=1,
            library_checksum="lib", params_fingerprint="params", chunk_size=2,
        )
        baseline = build_cache_key(**common)
        self.assertNotEqual(baseline, build_cache_key(**{**common, "line_no": 1}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "image_hash": "x"}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "boxes_summary": "x"}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "library_version": 9}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "library_checksum": "x"}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "params_fingerprint": "x"}))
        self.assertNotEqual(baseline, build_cache_key(**{**common, "chunk_size": 4}))

    def test_library_upgrade_invalidates_only_affected_entries(self):
        _result, first = self._process()
        affected = sorted(first["recompute_lines"])
        removed = self.engine.cache.invalidate(
            library_versions={self.library.library_version},
            library_checksums={self.library.checksum},
        )
        self.assertTrue(removed)
        upgraded = build_library("cs-v1", 2)
        upgraded_engine = OCREngine(upgraded, self.params, self.engine.cache)
        _result, report = upgraded_engine.process_page(
            self.page, chunk_size=2, latency_budget_ms=50.0,
            memory_ceiling_bytes=200_000,
        )
        self.assertEqual(set(report["recompute_lines"]) & set(report["reused_lines"]), set())
        self.assertEqual(
            set(report["recompute_lines"]) | set(report["reused_lines"]),
            set(affected),
        )

    def test_cache_hit_does_not_change_output(self):
        result_a, _ = self._process()
        result_b, _ = self._process()
        self.assertEqual(result_a.result_hash(), result_b.result_hash())


if __name__ == "__main__":
    unittest.main()
