import unittest

from dungeon_render.errors import DungeonError
from dungeon_render.validation import parse_level, parse_task


def make_level(**overrides):
    level = {
        "width": 8,
        "height": 6,
        "rooms": [
            {"id": 0, "cells": [[1, 1], [2, 1], [1, 2], [2, 2]]},
            {"id": 1, "cells": [[5, 1], [6, 1], [5, 2], [6, 2]]},
        ],
        "corridors": [[3, 1], [4, 1]],
        "doors": [[3, 1]],
        "wall_thickness": 1,
        "light_radius": 5.0,
        "palette": [[0.0, 40], [0.5, 120], [1.0, 220]],
    }
    level.update(overrides)
    return level


def make_task(**overrides):
    task = {
        "output_width": 16,
        "output_height": 12,
        "cell_pixels": 2,
        "tile_cols": 2,
        "tile_rows": 2,
        "concurrency": 2,
        "in_flight_pixel_limit": 1000,
        "round_budget": 1000,
        "tolerance": 0,
        "state_dir": "work/test_state",
        "output_dir": "work/test_output",
    }
    task.update(overrides)
    return task


class ValidationTests(unittest.TestCase):
    def assert_schema(self, payload, parser, *args):
        with self.assertRaises(DungeonError) as caught:
            parser(payload, *args)
        self.assertEqual(caught.exception.code, "E_SCHEMA")

    def test_valid_documents_parse(self):
        level = parse_level(make_level())
        task = parse_task(make_task(), level)
        self.assertEqual((task.output_width, task.output_height), (16, 12))
        self.assertIn((3, 1), level.walkable())

    def test_dimensions_must_be_positive(self):
        self.assert_schema(make_level(width=0), parse_level)
        self.assert_schema(make_level(height=-2), parse_level)
        self.assert_schema(make_level(width=True), parse_level)

    def test_cells_must_stay_inside_grid(self):
        bad = make_level()
        bad["rooms"][0]["cells"].append([8, 1])
        self.assert_schema(bad, parse_level)

    def test_duplicate_cells_rejected(self):
        bad = make_level()
        bad["corridors"] = [[3, 1], [3, 1]]
        self.assert_schema(bad, parse_level)
        overlap = make_level(corridors=[[1, 1]])
        self.assert_schema(overlap, parse_level)

    def test_door_must_be_walkable_and_near_wall(self):
        # A 3x3 room has a true interior cell (2,2) with only walkable
        # neighbors, which cannot satisfy the door adjacency rule.
        no_wall = make_level(
            doors=[[2, 2]],
            rooms=[
                {
                    "id": 0,
                    "cells": [
                        [1, 1], [2, 1], [3, 1],
                        [1, 2], [2, 2], [3, 2],
                        [1, 3], [2, 3], [3, 3],
                    ],
                },
                {"id": 1, "cells": [[5, 1], [6, 1], [5, 2], [6, 2]]},
            ],
        )
        self.assert_schema(no_wall, parse_level)
        on_wall = make_level(doors=[[0, 0]])
        self.assert_schema(on_wall, parse_level)

    def test_wall_thickness_bounded_by_smallest_room(self):
        self.assert_schema(make_level(wall_thickness=5), parse_level)

    def test_light_radius_finite_positive(self):
        self.assert_schema(make_level(light_radius=0), parse_level)
        self.assert_schema(make_level(light_radius=float("inf")), parse_level)

    def test_palette_must_be_monotone_and_span_unit_range(self):
        self.assert_schema(
            make_level(palette=[[0.0, 200], [1.0, 100]]), parse_level
        )
        self.assert_schema(
            make_level(palette=[[0.1, 40], [1.0, 220]]), parse_level
        )
        self.assert_schema(
            make_level(palette=[[0.0, 40], [0.5, 999]]), parse_level
        )

    def test_tiles_must_cover_resolution(self):
        level = parse_level(make_level())
        self.assert_schema(make_task(tile_cols=3), parse_task, level)
        self.assert_schema(
            make_task(output_width=15, output_height=12), parse_task, level
        )

    def test_memory_limit_must_hold_one_tile(self):
        level = parse_level(make_level())
        self.assert_schema(
            make_task(in_flight_pixel_limit=10), parse_task, level
        )

    def test_boolean_rejected_as_integer(self):
        self.assert_schema(make_level(wall_thickness=True), parse_level)


if __name__ == "__main__":
    unittest.main()
