"""Requirement 2: undersized frames are centred on real canvas-coloured pixels."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.spec import parse_spec
from tests.helpers import copy_sample


class PaddingTests(unittest.TestCase):
    def test_small_frame_padding_is_real_background_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            spec = parse_spec(spec_path)
            batch = build_batch(spec)

            # Index 1 is 160x90 on a 320x180 canvas -> 80/45 px on each side.
            result = next(frame for frame in batch.frames if frame.spec.index == 1)
            self.assertTrue(result.succeeded)
            composited = result.composited
            assert composited is not None
            self.assertEqual(composited.placement, "centered")
            self.assertEqual(composited.content_box, (80, 45, 240, 135))

            image = composited.image
            self.assertEqual(image.size, (320, 180))
            self.assertEqual(image.mode, "RGBA")
            background = spec.background.rgba
            checks = [
                (0, 0),
                (319, 0),
                (0, 179),
                (319, 179),
                (79, 90),
                (240, 90),
                (160, 44),
                (160, 135),
            ]
            for point in checks:
                self.assertEqual(image.getpixel(point), background, point)

            # The frame's own visible content survived, so padding is not a
            # uniform fill over the whole canvas.
            inner_pixels = {
                image.getpixel((x, y))
                for y in range(46, 134, 7)
                for x in range(81, 239, 7)
            }
            self.assertGreater(len(inner_pixels), 5)

    def test_small_rgb_frame_edges_use_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            spec = parse_spec(spec_path)
            batch = build_batch(spec)
            # Index 8: 300x170 RGB, 10px horizontal and 5px vertical padding.
            result = next(frame for frame in batch.frames if frame.spec.index == 8)
            composited = result.composited
            assert composited is not None
            self.assertEqual(composited.content_box, (10, 5, 310, 175))
            image = composited.image
            self.assertEqual(image.getpixel((0, 0)), spec.background.rgba)
            self.assertEqual(image.getpixel((5, 90)), spec.background.rgba)
            self.assertEqual(image.getpixel((314, 90)), spec.background.rgba)
            self.assertEqual(image.getpixel((160, 2)), spec.background.rgba)
            self.assertEqual(image.getpixel((160, 177)), spec.background.rgba)


if __name__ == "__main__":
    unittest.main()
