import math
import unittest

import numpy as np

from subpixel.geometry import (
    capsule_cell_area_polygon,
    capsule_polygon,
    clip_polygon_rect,
    disk_polygon,
    liang_barsky_clip,
    point_in_polygon,
    segment_grid_cells,
    strip_cell_area,
    _polygon_area,
)


class GeometryTest(unittest.TestCase):
    def test_grid_traversal_covers_every_cell(self):
        cells = segment_grid_cells(0.5, 0.5, 4.5, 3.5)
        visited = {(cell.ix, cell.iy) for cell in cells}
        self.assertIn((0, 0), visited)
        self.assertIn((4, 3), visited)
        spans = sum(cell.span for cell in cells)
        self.assertAlmostEqual(spans, 1.0, places=9)

    def test_liang_barsky_clipping(self):
        clipped = liang_barsky_clip(2.2, 2.3, 5.8, 5.9, 3.0, 3.0, 4.0, 4.0)
        self.assertIsNotNone(clipped)
        x0, y0, x1, y1, t0, t1 = clipped
        self.assertTrue(3.0 <= x0 <= 4.0 and 3.0 <= y0 <= 4.0)
        self.assertTrue(3.0 <= x1 <= 4.0 and 3.0 <= y1 <= 4.0)
        self.assertGreater(t1, t0)

    def test_capsule_area_matches_formula(self):
        x0, y0, x1, y1, radius = 1.2, 1.3, 6.4, 3.9, 0.42
        poly = capsule_polygon(x0, y0, x1, y1, radius, steps=64)
        length = math.hypot(x1 - x0, y1 - y0)
        expected = 2 * radius * length + math.pi * radius * radius
        self.assertAlmostEqual(_polygon_area(poly), expected, places=3)

    def test_cell_area_fully_contained(self):
        poly = capsule_polygon(3.2, 3.3, 3.7, 3.5, 0.08, steps=32)
        clipped = clip_polygon_rect(poly, 3.0, 3.0, 4.0, 4.0)
        self.assertAlmostEqual(
            _polygon_area(clipped), _polygon_area(poly), places=4)

    def test_quarter_disk_at_corner(self):
        disk = disk_polygon(5.0, 5.0, 0.5, steps=64)
        clipped = clip_polygon_rect(disk, 5.0, 5.0, 6.0, 6.0)
        self.assertAlmostEqual(
            _polygon_area(clipped), math.pi * 0.25 / 4, places=3)

    def test_strip_and_capsule_agree_on_full_cell(self):
        strip = strip_cell_area(3.2, 3.3, 3.8, 3.6, 0.1, 3, 3)
        capsule = capsule_cell_area_polygon(
            3.2, 3.3, 3.8, 3.6, 0.1, 3, 3, steps=32)
        self.assertLess(abs(strip - capsule), 0.05)

    def test_point_in_polygon(self):
        polygon = np.array([(0, 0), (4, 0), (4, 4), (0, 4)], dtype=float)
        points = np.array([(2, 2), (5, 5), (-0.1, 2), (3.9, 0.1)])
        result = point_in_polygon(points, polygon)
        self.assertEqual(result.tolist(), [True, False, False, True])


if __name__ == "__main__":
    unittest.main()
