from __future__ import annotations

import unittest

from hdr_color.color import (
    ColorPipeline,
    bradford_adaptation,
    build_rgb_to_xyz,
    invert3,
    mat_mul,
    max_abs_matrix_error,
)


IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class ColourPipelineTests(unittest.TestCase):
    def test_matrix_is_derived_and_invertible(self) -> None:
        matrix = build_rgb_to_xyz(
            ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)),
            (0.3127, 0.3290),
        )
        self.assertLess(
            max_abs_matrix_error(mat_mul(invert3(matrix), matrix), IDENTITY),
            1e-12,
        )
        # The BT.709 red primary has X/Z ratios implied by its chromaticity.
        red = (matrix[0][0], matrix[1][0], matrix[2][0])
        self.assertAlmostEqual(red[0] / sum(red), 0.64, places=12)
        self.assertAlmostEqual(red[1] / sum(red), 0.33, places=12)

    def test_white_adaptation_is_reversible(self) -> None:
        adaptation = bradford_adaptation((0.3127, 0.3290), (0.3457, 0.3585))
        self.assertLess(
            max_abs_matrix_error(
                mat_mul(invert3(adaptation), adaptation), IDENTITY
            ),
            1e-12,
        )

    def test_bt2020_to_bt709_roundtrip_and_white(self) -> None:
        pipeline = ColorPipeline.create(
            "BT2020",
            ("D65", (0.3127, 0.3290)),
            "BT709",
            ("D65", (0.3127, 0.3290)),
        )
        white = pipeline.convert_rgb([1.0, 1.0, 1.0])
        for channel in white:
            self.assertAlmostEqual(channel, 1.0, places=12)
        restored = pipeline.reverse_rgb(pipeline.convert_rgb([0.2, 0.7, 1.3]))
        for original, value in zip([0.2, 0.7, 1.3], restored):
            self.assertAlmostEqual(original, value, places=11)
        self.assertLess(
            pipeline.report()["roundtrip_matrix_max_abs_error"],
            1e-12,
        )

    def test_d65_to_d50_adaptation_preserves_luminance(self) -> None:
        pipeline = ColorPipeline.create(
            "BT709",
            ("D65", (0.3127, 0.3290)),
            "BT709",
            ("D50", (0.3457, 0.3585)),
        )
        converted = pipeline.convert_rgb([1.0, 1.0, 1.0])
        self.assertAlmostEqual(pipeline.luminance(converted), 1.0, places=9)

    def test_wide_gamut_pixels_are_detected_and_desaturated(self) -> None:
        pipeline = ColorPipeline.create(
            "BT2020",
            ("D65", (0.3127, 0.3290)),
            "BT709",
            ("D65", (0.3127, 0.3290)),
        )
        converted = pipeline.convert_rgb([0.0, 4.0, 0.0])
        self.assertTrue(pipeline.is_out_of_gamut(converted))
        before = pipeline.luminance(converted)
        corrected, changed = pipeline.desaturate_to_gamut(converted)
        self.assertTrue(changed)
        self.assertTrue(all(channel >= 0.0 for channel in corrected))
        self.assertAlmostEqual(pipeline.luminance(corrected), before, places=9)


if __name__ == "__main__":
    unittest.main()
