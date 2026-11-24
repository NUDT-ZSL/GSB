import unittest

import numpy as np

from dungeon_render.raster import Renderer
from dungeon_render.tiles import build_tiles
from tests.test_validation import make_level, make_task
from dungeon_render.validation import parse_level, parse_task
from dataclasses import replace


class RasterTests(unittest.TestCase):
    def setUp(self):
        self.level = parse_level(make_level())
        self.task = parse_task(make_task(), self.level)
        self.renderer = Renderer(self.level, self.task)

    def test_tile_assembly_equals_single_tile_render(self):
        whole_gray, whole_outline = self.renderer.render_whole()
        gray = np.zeros_like(whole_gray)
        outline = np.zeros_like(whole_outline)
        for tile in build_tiles(self.task):
            tile_gray, tile_outline = self.renderer.render_region(tile)
            gray[tile.y : tile.y + tile.height, tile.x : tile.x + tile.width] = tile_gray
            outline[
                tile.y : tile.y + tile.height, tile.x : tile.x + tile.width
            ] = tile_outline
        np.testing.assert_array_equal(gray, whole_gray)
        np.testing.assert_array_equal(outline, whole_outline)

    def test_rendering_is_deterministic(self):
        tile = build_tiles(self.task)[1]
        first_gray, first_outline = self.renderer.render_region(tile)
        second_gray, second_outline = self.renderer.render_region(tile)
        np.testing.assert_array_equal(first_gray, second_gray)
        np.testing.assert_array_equal(first_outline, second_outline)

    def test_gray_values_quantized_to_uint8_range(self):
        gray, _ = self.renderer.render_whole()
        self.assertEqual(gray.dtype, np.uint8)
        self.assertGreaterEqual(int(gray.max()), 0)
        self.assertLessEqual(int(gray.max()), 255)

    def test_unaligned_tile_boundaries_stay_seam_free(self):
        # Bypass the divisibility schema rule to prove the raster itself is
        # independent of tile origin even for ragged, non-cell-aligned crops.
        task = replace(
            self.task,
            tile_cols=3,
            tile_rows=2,
            in_flight_pixel_limit=100,
        )
        renderer = Renderer(self.level, task)
        whole_gray, whole_outline = renderer.render_whole()
        gray = np.zeros_like(whole_gray)
        outline = np.zeros_like(whole_outline)
        for tile in build_tiles(task):
            tile_gray, tile_outline = renderer.render_region(tile)
            gray[tile.y : tile.y + tile.height, tile.x : tile.x + tile.width] = tile_gray
            outline[
                tile.y : tile.y + tile.height, tile.x : tile.x + tile.width
            ] = tile_outline
        np.testing.assert_array_equal(gray, whole_gray)
        np.testing.assert_array_equal(outline, whole_outline)

    def test_door_opening_appears_on_both_sides_of_wall(self):
        # Door cell (3,1) touches the wall cell (3,0).  The wall-facing stroke
        # must be suppressed on the door side while ordinary wall cells keep
        # theirs, proving the opening is driven by whole-grid adjacency.
        gray, _ = self.renderer.render_whole()
        cell_pixels = self.task.cell_pixels
        door_top = gray[cell_pixels, cell_pixels * 3 : cell_pixels * 4]
        self.assertNotIn(25, np.unique(door_top).tolist())
        # A non-door corridor cell adjacent to a wall retains the edge stroke.
        wall_edge = gray[cell_pixels, cell_pixels * 4 : cell_pixels * 5]
        self.assertIn(25, np.unique(wall_edge).tolist())


if __name__ == "__main__":
    unittest.main()
