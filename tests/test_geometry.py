from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from volumetric_engine.geometry import (
    build_interval_chain,
    ray_box_interval,
    segment_optical_depth,
)
from volumetric_engine.model import MediaBox
from volumetric_engine.validation import load_scene

from .helpers import write_scene


def medium(
    medium_id: str,
    center: tuple[float, float, float],
    size: tuple[float, float, float],
    ior: float,
    index: int,
    extinction: float = 0.5,
) -> MediaBox:
    return MediaBox(
        id=medium_id,
        kind="test",
        center=center,
        size=size,
        extinction=(extinction, extinction, extinction),
        albedo=(0.5, 0.5, 0.5),
        ior=ior,
        declaration_index=index,
    )


class GeometryTests(unittest.TestCase):
    def test_ray_box_interval(self) -> None:
        box = medium("box", (1.0, 0.0, 0.0), (2.0, 2.0, 2.0), 1.0, 0)
        interval = ray_box_interval((-3.0, 0.0, 0.0), (1.0, 0.0, 0.0), box)
        self.assertIsNotNone(interval)
        assert interval is not None
        self.assertAlmostEqual(interval[0], 3.0)
        self.assertAlmostEqual(interval[1], 5.0)
        self.assertIsNone(ray_box_interval((0.0, 3.0, 0.0), (1.0, 0.0, 0.0), box))

    def test_overlap_priority_ior_then_declaration(self) -> None:
        low = medium("low", (0.0, 0.0, 0.0), (6.0, 6.0, 6.0), 1.0, 0)
        high = medium("high", (1.0, 0.0, 0.0), (2.0, 2.0, 2.0), 1.5, 1)
        chain = build_interval_chain((-5.0, 0.0, 0.0), (1.0, 0.0, 0.0), (low, high))
        owners = [(round(item.start, 6), round(item.end, 6), item.medium.id) for item in chain]
        self.assertEqual(
            owners,
            [(2.0, 5.0, "low"), (5.0, 7.0, "high"), (7.0, 8.0, "low")],
        )
        tie_early = medium("early", (1.0, 0.0, 0.0), (2.0, 2.0, 2.0), 1.5, 1)
        tie_late = medium("late", (1.0, 0.0, 0.0), (2.0, 2.0, 2.0), 1.5, 2)
        tie_chain = build_interval_chain((-5.0, 0.0, 0.0), (1.0, 0.0, 0.0), (tie_early, tie_late))
        self.assertEqual(tie_chain[0].medium.id, "early")

    def test_midpoint_ownership_stable_at_boundary(self) -> None:
        first = medium("first", (-1.0, 0.0, 0.0), (2.0, 4.0, 4.0), 1.0, 0)
        second = medium("second", (1.0, 0.0, 0.0), (2.0, 4.0, 4.0), 1.0, 1)
        chain = build_interval_chain((-4.0, 0.0, 0.0), (1.0, 0.0, 0.0), (first, second))
        self.assertEqual([item.medium.id for item in chain], ["first", "second"])
        self.assertAlmostEqual(chain[0].end, 4.0)
        # Both ownership results are determined from an interior midpoint, not
        # from querying the shared x=0 endpoint directly.
        self.assertEqual(chain[0].medium.declaration_index, 0)
        self.assertEqual(chain[1].medium.declaration_index, 1)

    def test_shadow_sweep_matches_chain(self) -> None:
        media = (
            medium("a", (0.0, 0.0, 0.0), (6.0, 4.0, 4.0), 1.0, 0, 0.4),
            medium("b", (1.0, 0.0, 0.0), (2.0, 3.0, 3.0), 1.4, 1, 0.9),
            medium("c", (-1.5, 0.0, 0.0), (1.0, 2.0, 2.0), 1.0, 2, 0.2),
        )
        origin = (-4.0, 0.1, 0.0)
        direction = (1.0, 0.0, 0.0)
        distance = 8.0
        chain = build_interval_chain(origin, direction, media, t_min=0.0, t_max=distance)
        expected = sum(item.length * item.medium.extinction_scalar for item in chain)
        actual = segment_optical_depth(origin, direction, media, distance)
        self.assertTrue(math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-12))

    def test_scene_media_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = load_scene(write_scene(Path(temp) / "case"))
            chain = build_interval_chain(
                scene.camera.position,
                scene.camera.forward,
                scene.media,
            )
            self.assertEqual(len(chain), 1)
            self.assertEqual(chain[0].medium.id, "medium_a")
            self.assertAlmostEqual(chain[0].start, 4.5)


if __name__ == "__main__":
    unittest.main()

