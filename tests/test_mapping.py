import unittest

import numpy as np

from domecalib.core.geometry import (
    make_basis,
    undistort_radial,
    distort_radial,
    validate_distortion,
    ray_sphere,
)
from domecalib.core.mapping import (
    reverse_map_pixels,
    ground_truth_lut,
    fit_tps,
    angular_distance,
    MIN_CONTROL_POINTS,
)
from domecalib.core.errors import (
    CalibrationError,
    E_INSUFFICIENT_POINTS,
    E_NEAR_COLLINEAR,
    E_DISTORTION_OUT_OF_RANGE,
    E_NO_DOME_INTERSECTION,
)
from domecalib.core.samplegen import generate_sample_project
from domecalib.core.models import ControlPointSet


class GeometryTests(unittest.TestCase):
    def test_basis_is_right_handed(self):
        basis = make_basis(np.array([0.0, 0.0, -1.0]), np.array([0.0, 1.0, 0.0]))
        self.assertAlmostEqual(np.linalg.det(basis), 1.0, places=12)

    def test_distortion_roundtrip(self):
        rng = np.random.default_rng(0)
        # Keep radii inside the monotone envelope of the tested coefficients.
        xd = rng.normal(size=(64, 64)) * 0.4
        yd = rng.normal(size=(64, 64)) * 0.4
        for k1, k2 in [(0.0, 0.0), (-0.05, 0.01), (0.03, -0.01)]:
            xu, yu = undistort_radial(xd, yd, k1, k2)
            xb, yb = distort_radial(xu, yu, k1, k2)
            np.testing.assert_allclose(xb, xd, atol=1e-10)
            np.testing.assert_allclose(yb, yd, atol=1e-10)

    def test_undistort_never_diverges_at_corner(self):
        # Large barrel distortion at a wide sensor corner.
        xd = np.full((5, 5), 2.0)
        yd = np.full((5, 5), 1.5)
        xu, yu = undistort_radial(xd, yd, -0.2, 0.05)
        self.assertTrue(np.all(np.isfinite(xu)))
        self.assertTrue(np.all(xu <= xd + 1e-9))

    def test_distortion_validation_rejects_extremes(self):
        with self.assertRaises(CalibrationError) as ctx:
            validate_distortion(0.9, 0.0, 1.5)
        self.assertEqual(ctx.exception.code, E_DISTORTION_OUT_OF_RANGE)

    def test_ray_sphere_nearest_root(self):
        origin = np.array([0.0, 0.0, 5.0])
        direction = np.array([0.0, 0.0, -1.0])
        points, hit = ray_sphere(origin, direction, np.zeros(3), 2.0)
        self.assertTrue(hit.all())
        # Nearest entry root at z = +2, not the far root z = -2.
        np.testing.assert_allclose(points[..., 2], 2.0, atol=1e-12)


class ReverseMappingTests(unittest.TestCase):
    def test_matches_ground_truth(self):
        project = generate_sample_project()
        for spec in project.projectors:
            lon, lat, valid = reverse_map_pixels(spec, project.dome)
            t_lon, t_lat, t_valid = ground_truth_lut(spec, project.dome)
            self.assertTrue(np.array_equal(valid, t_valid))
            err = angular_distance(lon, lat, t_lon, t_lat)[valid & t_valid]
            self.assertLess(err.max(), 5e-7)

    def test_invalid_pixels_are_masked_and_nan(self):
        project = generate_sample_project()
        spec = project.projectors[0]
        lon, lat, valid = reverse_map_pixels(spec, project.dome)
        self.assertTrue(np.all(np.isnan(lon[~valid])))
        self.assertTrue(np.all(np.isnan(lat[~valid])))
        self.assertGreater(valid.mean(), 0.5)

    def test_no_overlap_projector_raises(self):
        project = generate_sample_project()
        spec = project.projectors[0]
        # Aim straight up while sitting directly above the dome pole, so the
        # whole frustum misses the upper hemisphere surface.
        spec.position = (0.0, 0.0, 30.0)
        spec.forward = (0.1, 0.0, 1.0)
        spec.up_hint = (0.0, 1.0, 0.0)
        with self.assertRaises(CalibrationError) as ctx:
            reverse_map_pixels(spec, project.dome)
        self.assertEqual(ctx.exception.code, E_NO_DOME_INTERSECTION)


class TPSDegradationTests(unittest.TestCase):
    def test_insufficient_points(self):
        pts = ControlPointSet("PX", np.zeros(3), np.zeros(3),
                              np.zeros(3), np.zeros(3), 0.0)
        with self.assertRaises(CalibrationError) as ctx:
            fit_tps(pts)
        self.assertEqual(ctx.exception.code, E_INSUFFICIENT_POINTS)

    def test_near_collinear_rejected(self):
        n = MIN_CONTROL_POINTS + 2
        x = np.linspace(0.0, 1.0, n)
        pts = ControlPointSet("PX", x, x * 1e-12, x, np.zeros(n), 0.0)
        with self.assertRaises(CalibrationError) as ctx:
            fit_tps(pts)
        self.assertEqual(ctx.exception.code, E_NEAR_COLLINEAR)

    def test_healthy_fit_bounded_and_cross_validated(self):
        project = generate_sample_project()
        points = project.control_points[0]
        field, quality = fit_tps(points)
        self.assertEqual(quality.status, "ok")
        self.assertTrue(np.isfinite(quality.loocv_rms_rad))
        # Correction field must never produce runaway coordinates.
        test = np.column_stack([points.observed_lon, points.observed_lat])
        corrected = field.evaluate(test)
        self.assertTrue(np.all(np.isfinite(corrected)))
        self.assertLess(np.abs(corrected - test).max(), 0.05 + 1e-9)


if __name__ == "__main__":
    unittest.main()
