import unittest

import numpy as np

from domecalib.core.samplegen import generate_sample_project


class SampleGeneratorTests(unittest.TestCase):
    def test_deterministic_same_seed(self):
        a = generate_sample_project(seed=42)
        b = generate_sample_project(seed=42)
        self.assertEqual(a.seed, b.seed)
        for pa, pb in zip(a.projectors, b.projectors):
            np.testing.assert_array_equal(pa.position, pb.position)
            self.assertEqual(pa.k1, pb.k1)
            self.assertEqual(pa.fov_h_deg, pb.fov_h_deg)
        for ca, cb in zip(a.control_points, b.control_points):
            np.testing.assert_array_equal(ca.observed_lon, cb.observed_lon)

    def test_different_seed_differs(self):
        a = generate_sample_project(seed=1)
        b = generate_sample_project(seed=2)
        self.assertNotAlmostEqual(
            a.projectors[0].k1, b.projectors[0].k1
        )

    def test_index_dependent_perturbation(self):
        project = generate_sample_project(seed=7)
        points = project.control_points[0]
        n = points.count()
        # Perturbation amplitude envelope grows with point index.
        displacement = np.hypot(
            points.observed_lon - points.ideal_lon,
            points.observed_lat - points.ideal_lat,
        )
        head = displacement[: n // 4].mean()
        tail = displacement[3 * n // 4 :].mean()
        self.assertGreater(tail, head)

    def test_grid_is_sparse_not_per_pixel(self):
        project = generate_sample_project()
        for points in project.control_points:
            # 10 x 8 calibration grid = 80 points per projector.
            self.assertEqual(points.count(), 80)


if __name__ == "__main__":
    unittest.main()
