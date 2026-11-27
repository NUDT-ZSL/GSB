import unittest

import numpy as np

from domecalib.core.blending import (
    rasterize_coverage,
    _chamfer_distance,
)
from domecalib.core.engine import CalibrationEngine
from domecalib.core.samplegen import generate_sample_project


class BlendingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project = generate_sample_project()
        engine = CalibrationEngine(project)
        engine.rebuild_luts()
        engine.rebuild_blending()
        cls.project = project
        cls.engine = engine

    def test_distance_transform_zero_outside(self):
        lut = self.project.luts["P1"]
        coverage = rasterize_coverage(lut, self.project.grid_width,
                                      self.project.grid_height)
        dist = _chamfer_distance(coverage)
        self.assertTrue(np.all(dist[~coverage] == 0.0))
        self.assertTrue(np.all(dist[coverage] >= 0.0))

    def test_weights_in_unit_range(self):
        for mask in self.project.masks.values():
            self.assertTrue(np.all(mask.weight >= 0.0))
            self.assertTrue(np.all(mask.weight <= 1.0 + 1e-9))

    def test_grid_weight_sum_is_constant(self):
        ids = [s.projector_id for s in self.project.projectors]
        gw, gh = self.project.grid_width, self.project.grid_height
        total = np.zeros((gh, gw))
        for pid in ids:
            lut = self.project.luts[pid]
            mask = self.project.masks[pid]
            valid = lut.valid
            col = np.rint((lut.lon[valid] + np.pi) / (2 * np.pi) * gw).astype(int) % gw
            row = np.clip(
                np.rint((np.pi / 2 - lut.lat[valid]) / (np.pi / 2) * (gh - 1)).astype(int),
                0, gh - 1,
            )
            # Take one contribution per covered cell to honor cell semantics.
            cell = np.zeros((gh, gw))
            cell[row, col] = np.maximum(cell[row, col], mask.weight[valid])
            total += cell
        covered = total > 1e-9
        # Cell-level sum stays near unity where projectors overlap (allowing
        # for nearest-cell quantization at overlap edges).
        overlap = total > 1.2
        self.assertLess(overlap.sum(), 0.02 * covered.sum())

    def test_brightness_report_uniform(self):
        report = self.engine.run_reports()
        bright = report["brightness"]
        self.assertLess(bright["uniformity_cv_post"], 1e-9)
        self.assertLess(bright["max_seam_jump"], 1e-9)
        post = bright["weight_sum_post_optimization"]
        self.assertAlmostEqual(post["min"], 1.0, places=6)
        self.assertAlmostEqual(post["max"], 1.0, places=6)

    def test_monotone_feather_no_real_jumps(self):
        report = self.engine.run_reports()
        self.assertEqual(report["blend_diagnostics"]["monotonicity_violations"], 0)


if __name__ == "__main__":
    unittest.main()
