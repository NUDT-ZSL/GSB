import os
import shutil
import tempfile
import unittest

import numpy as np

from ocrbench import pipeline
from ocrbench.config import PipelineConfig
from ocrbench.deterministic import content_hash
from ocrbench.errors import ErrorCode, OcrBenchError
from ocrbench.samplegen import generate_page
from ocrbench.templates import build_library
from ocrbench.workbench import OcrWorkbench


class DeterminismTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_pixels_are_seed_deterministic(self):
        first = generate_page("p", 777)
        second = generate_page("p", 777)
        np.testing.assert_array_equal(first.image, second.image)
        self.assertEqual(
            [line.to_dict() for line in first.lines],
            [line.to_dict() for line in second.lines],
        )

    def test_node_outputs_byte_identical_across_runs(self):
        wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())
        wb.create_dataset([("p1", 11), ("p2", 22)])
        first = wb.run_all()
        second = wb.run_all()
        for page_id in first:
            for node in ("preprocess", "detect", "features", "match", "recognize", "aggregate"):
                self.assertEqual(
                    content_hash(first[page_id].payloads[node]),
                    content_hash(second[page_id].payloads[node]),
                )
            # Second run must be a full cache hit.
            self.assertEqual(second[page_id].recomputed_nodes(), [])

    def test_detect_empty_is_attributed(self):
        config = PipelineConfig()
        blank = np.full((40, 60), 255, dtype=np.uint8)
        pre = pipeline.preprocess(blank, config)
        with self.assertRaises(OcrBenchError) as ctx:
            pipeline.detect_lines(pre, "page-blank")
        self.assertEqual(ctx.exception.code, ErrorCode.DETECT_EMPTY)
        self.assertEqual(ctx.exception.page_id, "page-blank")
        self.assertEqual(ctx.exception.stage, "detect")

    def test_match_failure_is_attributed(self):
        page = generate_page("p", 5)
        config = PipelineConfig()
        pre = pipeline.preprocess(page.image, config)
        detected = pipeline.detect_lines(pre, "p")
        features = pipeline.extract_features(pre, detected, config)
        # Wipe cells to force a matching-stage failure.
        for line in features["lines"]:
            line["cell_count"] = 0
            line["features"] = []
        library = build_library(1)
        with self.assertRaises(OcrBenchError) as ctx:
            pipeline.match_lines(features, library, config, "p")
        self.assertEqual(ctx.exception.code, ErrorCode.MATCH_FAILED)
        self.assertEqual(ctx.exception.stage, "match")


if __name__ == "__main__":
    unittest.main()
