"""Requirement 4: oversized frames are scaled down proportionally, not cropped."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.compose import resize_within
from frame_export.report import build_report
from frame_export.spec import parse_spec
from tests.helpers import copy_sample


class OversizeTests(unittest.TestCase):
    def test_large_frame_is_fully_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            spec = parse_spec(spec_path)
            batch = build_batch(spec)
            report = build_report(batch)

            # Index 2: 480x270 -> 320x180, exactly the canvas, fully visible.
            result = next(frame for frame in batch.frames if frame.spec.index == 2)
            composited = result.composited
            assert composited is not None
            self.assertEqual(composited.placement, "downscaled")
            self.assertEqual(composited.content_box, (0, 0, 320, 180))
            self.assertAlmostEqual(composited.scale, 320 / 480, places=4)
            entry = report["frames"][2]
            self.assertEqual(entry["size_processing"]["scale"], round(320 / 480, 6))

    def test_no_content_is_cropped_for_non_matching_aspect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            # Index 14: 500x90 wide frame -> 320x58 approx, letterboxed.
            result = next(frame for frame in batch.frames if frame.spec.index == 14)
            composited = result.composited
            assert composited is not None
            left, top, right, bottom = composited.content_box
            self.assertEqual(left, 0)
            self.assertEqual(right, 320)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(bottom, 180)
            # The full source width maps into the output: no horizontal crop,
            # and the aspect ratio is preserved to within one output pixel.
            self.assertLessEqual(
                abs(
                    (right - left) / (bottom - top)
                    - composited.source_width / composited.source_height
                ),
                0.15,
            )

    def test_portrait_frame_fits_without_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            # Index 15: 180x320 portrait.
            result = next(frame for frame in batch.frames if frame.spec.index == 15)
            composited = result.composited
            assert composited is not None
            left, top, right, bottom = composited.content_box
            self.assertEqual((top, bottom), (0, 180))
            self.assertGreaterEqual(right - left, 1)
            self.assertEqual(
                round((right - left) / 180, 2),
                round((bottom - top) / 320, 2),
            )

    def test_resize_helper_preserves_extreme_aspect(self) -> None:
        from PIL import Image

        image = Image.new("RGB", (1000, 10), (1, 2, 3))
        scaled = resize_within(image, *self._box(1000, 10, 320, 180), "nearest")
        self.assertGreaterEqual(scaled.width, 1)
        self.assertGreaterEqual(scaled.height, 1)
        self.assertTrue(scaled.width <= 320 and scaled.height <= 180)

    @staticmethod
    def _box(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
        scale = min(max_w / w, max_h / h)
        return max(1, round(w * scale)), max(1, round(h * scale))


if __name__ == "__main__":
    unittest.main()
