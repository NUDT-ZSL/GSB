from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from soundfield.images import cell_key, enumerate_images, reflect_position
from soundfield.scene import load_scene

from .conftest import make_workspace


class ImageEnumerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = make_workspace(self.tmp)
        self.scene = load_scene(str(self.ws["scene"]))

    def test_enumeration_deterministic_and_dedup(self) -> None:
        first = enumerate_images(self.scene)
        second = enumerate_images(self.scene)
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            self.assertEqual(a, b)
        cells = [(img.source_index, img.cell) for img in first]
        self.assertEqual(len(cells), len(set(cells)))

    def test_lexicographic_ordering(self) -> None:
        images = enumerate_images(self.scene)
        keys = [(img.order, img.walls, img.source_id) for img in images]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([img.image_index for img in images], list(range(len(images))))

    def test_order_bounds(self) -> None:
        images = enumerate_images(self.scene)
        self.assertTrue(all(img.order <= self.scene.reconstruction_order for img in images))
        direct = [img for img in images if img.order == 0]
        self.assertEqual(len(direct), len(self.scene.sources))

    def test_reflection_geometry(self) -> None:
        import numpy as np

        pos = np.asarray([1.0, 1.0, 1.0])
        reflected = reflect_position(pos, "x+", self.scene.room_size)
        self.assertAlmostEqual(reflected[0], 9.0)
        key = cell_key(reflected, self.scene.room_size)
        self.assertEqual(key[0], 2)

    def test_absorption_chain_history(self) -> None:
        images = enumerate_images(self.scene)
        order2 = [img for img in images if img.order == 2]
        self.assertTrue(order2)
        for img in order2:
            self.assertEqual(len(img.walls), 2)
            # 每频段吸声乘积必须严格小于 1（开区间系数累乘）。
            for band in range(self.scene.band_count):
                self.assertLess(img.absorption_f[band], 1.0)
                self.assertGreater(img.absorption_f[band], 0.0)


if __name__ == "__main__":
    unittest.main()
