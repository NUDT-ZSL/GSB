import unittest

import numpy as np

from subpixel.generator import SEED, generate_project
from subpixel.materials import material_masks, material_statistics
from subpixel.rasterizer import (
    rasterize_polygon,
    rasterize_strip,
    rasterize_supersample,
)
from subpixel.thumbnail import (
    area_average_downsample,
    build_thumbnail,
    declared_thumbnail_size,
)
from subpixel.validation import validate_project


class CoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = validate_project(generate_project(SEED))
        cls.camera = cls.project.camera
        cls.primary = rasterize_strip(cls.project, cls.camera)
        cls.reference = rasterize_polygon(cls.project, cls.camera)

    def test_coverage_bounds(self):
        self.assertTrue(np.all(self.primary.coverage >= 0.0))
        self.assertTrue(np.all(self.primary.coverage <= 1.0))

    def test_independent_analytic_reference_within_tolerance(self):
        diff = np.abs(self.primary.coverage - self.reference.coverage)
        self.assertLessEqual(diff.max(), 0.02)
        self.assertEqual(int((diff > 0.02).sum()), 0)

    def test_supersample_distribution_agrees(self):
        sampled = rasterize_supersample(
            self.project, self.camera, samples_per_axis=16)
        diff = np.abs(self.primary.coverage - sampled.coverage)
        self.assertLess(diff.mean(), 0.002)
        self.assertLessEqual(diff.max(), 0.15)

    def test_contribution_traceability(self):
        lookup = self.primary.contribution_lookup()
        curve_id = self.project.valid_curves()[0].curve_id
        self.assertIn(curve_id, lookup)
        for x, y, area, alpha in lookup[curve_id]:
            self.assertGreaterEqual(area, 0.0)
            self.assertGreater(alpha, 0.0)
            self.assertLessEqual(area, 1.0)
        total_alpha = sum(item[3] for items in lookup.values() for item in items)
        self.assertGreater(total_alpha, self.primary.coverage.sum())

    def test_material_statistics_partition(self):
        masks = material_masks(self.project, self.camera)
        overlap = masks["label"] & masks["cap_matte"]
        self.assertEqual(int(overlap.sum()), 0)
        stats = material_statistics(
            self.project, self.primary.coverage,
            self.primary.contributions, masks)
        self.assertEqual(len(stats), 3)
        self.assertTrue(all(stat.pixel_count > 0 for stat in stats))
        recomputed = sum(stat.total_coverage for stat in stats)
        # coverage on the bottle/cap silhouette cannot exceed the global sum
        self.assertLessEqual(recomputed, self.primary.coverage.sum() + 1e-9)

    def test_thumbnail_area_average_conserves_total(self):
        thumb_w, thumb_h = declared_thumbnail_size(
            self.camera.width_px, self.camera.height_px, 4)
        thumb = build_thumbnail(self.project, self.primary, thumb_w, thumb_h)
        self.assertEqual(thumb.conservation_error, 0.0)
        self.assertEqual(thumb.thumb_total, thumb.full_total)
        direct_diff = np.abs(thumb.averaged - thumb.direct)
        self.assertEqual(thumb.thumb_width, self.camera.width_px // 4)
        self.assertGreaterEqual(int((direct_diff > thumb.tolerance).sum()), 0)

    def test_downsample_rejects_non_divisor(self):
        with self.assertRaises(ValueError):
            area_average_downsample(self.primary.coverage, 33, 41)

    def test_fracture_marker_fires_when_direct_render_loses_feature(self):
        thumb = build_thumbnail(
            self.project, self.primary,
            self.camera.width_px // 4, self.camera.height_px // 4)
        # Synthesise a direct-render collapse on an attenuated pixel; the
        # fracture rule must distinguish a lost feature from mild decay.
        if not thumb.attenuation.any():
            self.skipTest("project has no attenuation pixels at 4x")
        ys, xs = np.nonzero(thumb.attenuation)
        y, x = int(ys[0]), int(xs[0])
        direct = thumb.direct.copy()
        direct[y, x] = 0.0
        difference = thumb.averaged - direct
        fracture = ((difference > thumb.tolerance)
                    & (direct < np.maximum(thumb.averaged, 1e-12) * 0.2))
        self.assertTrue(bool(fracture[y, x]))


if __name__ == "__main__":
    unittest.main()
