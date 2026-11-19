import unittest

import numpy as np

from ocr_pipeline.chunking import run_chunked, run_whole_page
from ocr_pipeline.detection import detect_lines, detection_stats
from ocr_pipeline.metrics import compute_metrics, recompute_from_per_line
from ocr_pipeline.recognition import RecognitionParams, collect_norm_stats
from ocr_pipeline.sample import generate_page
from ocr_pipeline.templates import build_library
from ocr_pipeline.util import array_hash, canonical_bytes


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.library = build_library("cs-v1", 1)
        self.params = RecognitionParams()
        self.page = generate_page("p1", 12345, self.library)

    def test_page_generation_is_byte_stable(self):
        again = generate_page("p1", 12345, self.library)
        self.assertTrue(np.array_equal(self.page.image, again.image))
        self.assertEqual(
            [line.box for line in self.page.truth],
            [line.box for line in again.truth],
        )
        self.assertEqual(array_hash(self.page.image), array_hash(again.image))

    def test_detection_is_deterministic_and_covers_truth(self):
        first = detect_lines(self.page.image)
        second = detect_lines(self.page.image)
        self.assertEqual(
            [d.box for d in first], [d.box for d in second]
        )
        stats = detection_stats(first, self.page.truth)
        self.assertEqual(stats["covered_truth_lines"], stats["truth_lines"])
        self.assertEqual(stats["coverage"], 1.0)
        # Integer pixel coordinates.
        for detection in first:
            self.assertTrue(all(isinstance(v, int) for v in detection.box))

    def test_whole_and_chunks_byte_identical(self):
        detections = detect_lines(self.page.image)
        norm = collect_norm_stats(self.page.image, detections)
        whole = run_whole_page(
            self.page.image, detections, self.library, self.params, norm
        )
        for chunk_size in (1, 2, 3, 4, 8):
            chunked, _ = run_chunked(
                self.page.image, detections, self.library, self.params,
                norm, chunk_size,
            )
            self.assertEqual(
                canonical_bytes(whole), canonical_bytes(chunked),
                f"chunk size {chunk_size} diverged",
            )

    def test_metrics_recompute(self):
        detections = detect_lines(self.page.image)
        norm = collect_norm_stats(self.page.image, detections)
        records = run_whole_page(
            self.page.image, detections, self.library, self.params, norm
        )
        metrics = compute_metrics(records, self.page.truth)
        rebuilt = recompute_from_per_line(metrics["per_line"])
        for key in ("line_accuracy", "char_accuracy", "correct_lines",
                    "total_lines", "correct_chars", "total_chars"):
            self.assertEqual(metrics[key], rebuilt[key])

    def test_library_version_changes_templates(self):
        upgraded = build_library("cs-v1", 2)
        self.assertNotEqual(self.library.checksum, upgraded.checksum)
        self.assertFalse(
            np.array_equal(self.library.templates, upgraded.templates)
        )


if __name__ == "__main__":
    unittest.main()
