from __future__ import annotations

import math
import unittest

from generate_samples import generate_frames


class SampleGeneratorTests(unittest.TestCase):
    def test_fixed_seed_is_deterministic_and_contains_edge_frames(self) -> None:
        first = generate_frames(20261118)
        second = generate_frames(20261118)
        self.assertEqual(first, second)
        self.assertEqual(first[0], [[0.0, 0.0, 0.0] for _ in range(32)])
        self.assertEqual(first[1], [[1.0, 1.0, 1.0] for _ in range(32)])
        self.assertTrue(
            all(
                math.isfinite(channel) and channel >= 0.0
                for frame in first
                for pixel in frame
                for channel in pixel
            )
        )
        self.assertGreater(max(channel for pixel in first[3] for channel in pixel), 1.0)


if __name__ == "__main__":
    unittest.main()
