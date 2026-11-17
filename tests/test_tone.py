from __future__ import annotations

import unittest

from hdr_color.errors import BusinessError
from hdr_color.tone import ToneMapper


class ToneMapperTests(unittest.TestCase):
    def test_curve_is_monotonic_and_bounded(self) -> None:
        mapper = ToneMapper.create("reinhard_extended", 100.0, 1000.0, 1.0)
        previous = -1.0
        for index in range(5001):
            mapped = mapper.map_luminance(index * 5.0)
            self.assertGreaterEqual(mapped + 1e-12, previous)
            self.assertLessEqual(mapped, 100.0)
            previous = mapped
        self.assertEqual(mapper.map_luminance(0.0), 0.0)
        self.assertEqual(mapper.map_luminance(1e12), 100.0)

    def test_luminance_scaling_preserves_channel_ratios(self) -> None:
        mapper = ToneMapper.create("reinhard_extended", 1000.0, 4000.0, 1.0)
        rgb = [50.0, 100.0, 25.0]
        luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        mapped, clipped = mapper.apply_to_rgb(rgb, luminance)
        self.assertFalse(clipped)
        ratios = [mapped[index] / rgb[index] for index in range(3)]
        self.assertAlmostEqual(ratios[0], ratios[1], places=12)
        self.assertAlmostEqual(ratios[1], ratios[2], places=12)

    def test_per_channel_clamp_is_reported(self) -> None:
        mapper = ToneMapper.create("linear_clip", 100.0, 100.0, 1.0)
        rgb = [90.0, 90.0, 150.0]
        luminance = sum(rgb) / 3.0
        mapped, clipped = mapper.apply_to_rgb(rgb, luminance)
        self.assertTrue(clipped)
        self.assertTrue(all(channel <= 100.0 for channel in mapped))

    def test_invalid_parameter_domains(self) -> None:
        with self.assertRaises(BusinessError):
            ToneMapper.create("unknown", 100.0, 100.0, 1.0)
        with self.assertRaises(BusinessError):
            ToneMapper.create("reinhard_extended", 100.0, 50.0, 1.0)
        with self.assertRaises(BusinessError):
            ToneMapper.create("reinhard_extended", 100.0, 200.0, 0.0)

    def test_black_frame_is_deterministic(self) -> None:
        mapper = ToneMapper.create("reinhard_extended", 100.0, 1000.0, 1.0)
        mapped, clipped = mapper.apply_to_rgb([0.0, 0.0, 0.0], 0.0)
        self.assertEqual(mapped, [0.0, 0.0, 0.0])
        self.assertFalse(clipped)


if __name__ == "__main__":
    unittest.main()
