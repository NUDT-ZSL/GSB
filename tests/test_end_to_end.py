import unittest

from ocr_pipeline.chunking import assert_byte_identical
from ocr_pipeline.engine import OCREngine
from ocr_pipeline.errors import (
    ChunkMismatchError,
    InconsistentError,
    InvalidInputError,
)
from ocr_pipeline.recognition import RecognitionParams
from ocr_pipeline.sample import generate_page
from ocr_pipeline.templates import build_library
from ocr_pipeline.validation import validate_full_page


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.library = build_library("cs-v1", 1)
        self.params = RecognitionParams()
        self.pages = [
            generate_page(f"page-{i}", 1000 + i, self.library)
            for i in range(3)
        ]
        self.engine = OCREngine(self.library, self.params)

    def test_full_pipeline_and_independent_validation(self):
        for page in self.pages:
            result, cache_report = self.engine.process_page(
                page, chunk_size=2, latency_budget_ms=50.0,
                memory_ceiling_bytes=200_000,
            )
            summary = validate_full_page(
                page, self.library, self.params, 2, self.engine.cache
            )
            self.assertEqual(len(summary["checks_passed"]), 6)
            self.assertIn("char_accuracy", result.metrics)
            self.assertEqual(
                cache_report["recompute_lines"] or cache_report["reused_lines"],
                sorted(cache_report["recompute_lines"] + cache_report["reused_lines"]),
            )

    def test_chunk_mismatch_is_reported_with_first_difference(self):
        page = self.pages[0]
        with self.assertRaises(ChunkMismatchError) as caught:
            assert_byte_identical(
                page.page_id,
                [{"line_no": 0, "text": "A", "box": [0, 0, 1, 1]}],
                [{"line_no": 0, "text": "B", "box": [0, 0, 1, 1]}],
            )
        self.assertEqual(caught.exception.code, "E_CHUNK_MISMATCH")
        self.assertEqual(caught.exception.context["page_id"], page.page_id)
        self.assertEqual(caught.exception.context["line_no"], 0)
        self.assertEqual(caught.exception.context["category"], "text")

    def test_invalid_input_error_code(self):
        with self.assertRaises(InvalidInputError) as caught:
            self.engine.query_line(self.pages[0], 999, latency_budget_ms=50.0)
        self.assertEqual(caught.exception.code, "E_INVALID_INPUT")

    def test_inconsistent_error_category(self):
        with self.assertRaises(InconsistentError) as caught:
            _raise_inconsistent()
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")
        self.assertEqual(caught.exception.context["category"], "synthetic")


def _raise_inconsistent():
    raise InconsistentError(
        "synthetic failure", {"category": "synthetic", "page_id": "page-0"}
    )


if __name__ == "__main__":
    unittest.main()
