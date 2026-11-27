import unittest

import numpy as np

from subpixel.contour import check_continuity
from subpixel.generator import SEED, generate_project
from subpixel.incremental import (
    CurveAdjustment,
    DensityAdjustment,
    apply_adjustments,
    incremental_recompute,
)
from subpixel.rasterizer import rasterize_strip
from subpixel.validation import validate_project


class IncrementalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = validate_project(generate_project(SEED))
        cls.baseline = rasterize_strip(cls.project, cls.project.camera)

    def test_incremental_matches_full_and_freezes_untouched(self):
        updated, changed = apply_adjustments(
            self.project,
            [CurveAdjustment("FB-0001", radius_px=1.3, opacity=0.95)],
            [DensityAdjustment(
                kind="grain", material="frosted_glass", surface="body",
                region=(-1.8, 0.4, -0.6, 1.2), add=5, remove=2)],
        )
        updated = validate_project(updated)
        result, report = incremental_recompute(
            updated, self.baseline, changed)
        full = rasterize_strip(updated, updated.camera)

        self.assertTrue(report.equal_to_full)
        self.assertEqual(report.untouched_max_error, 0.0)
        self.assertTrue(np.allclose(result.coverage, full.coverage, atol=1e-12))
        self.assertGreater(len(report.affected_pixels), 0)
        self.assertLess(len(report.affected_pixels),
                        result.width * result.height // 4)
        self.assertLess(report.elapsed_ms,
                        report.full_recompute_elapsed_ms)
        self.assertEqual(updated.version, "1.0.1")

    def test_unchanged_project_keeps_pixels_identical(self):
        updated, changed = apply_adjustments(self.project, [], [])
        result, report = incremental_recompute(
            updated, self.baseline, changed)
        self.assertEqual(report.affected_pixels, [])
        np.testing.assert_array_equal(result.coverage, self.baseline.coverage)


class ContourTest(unittest.TestCase):
    def test_rim_gap_is_located_with_responsible_fibres(self):
        project = validate_project(generate_project(SEED))
        result = rasterize_strip(project, project.camera)
        report = check_continuity(project, result, threshold=0.05)
        self.assertEqual(len(report.breaks), 1)
        gap = report.breaks[0]
        self.assertEqual(gap.side, "left")
        self.assertEqual(gap.coverage, 0.0)
        self.assertIn("RM-0010", gap.curve_ids)
        self.assertIn("RM-0012", gap.curve_ids)
        self.assertIn("missing-link", gap.curve_ids)


if __name__ == "__main__":
    unittest.main()
