"""Requirement 3: RGB frames without alpha become fully opaque and are flagged."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.report import build_report
from frame_export.spec import parse_spec
from tests.helpers import copy_sample


class ChannelTests(unittest.TestCase):
    def test_rgb_frame_is_opaque_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)

            # Index 3 is 320x180 RGB in the sample.
            result = next(frame for frame in batch.frames if frame.spec.index == 3)
            self.assertTrue(result.succeeded)
            composited = result.composited
            assert composited is not None
            self.assertEqual(composited.source_mode, "RGB")
            self.assertFalse(composited.has_alpha)
            self.assertEqual(composited.image.mode, "RGBA")
            self.assertEqual(composited.image.getchannel("A").getextrema(), (255, 255))

            entry = report["frames"][3]
            self.assertEqual(entry["channel"]["source_mode"], "RGB")
            self.assertFalse(entry["channel"]["has_alpha_channel"])
            self.assertTrue(entry["channel"]["fully_opaque"])
            self.assertTrue(
                any("缺少透明通道" in note for note in entry["notes"]),
                entry["notes"],
            )

    def test_rgba_frame_keeps_transparency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            # Index 0 is a full-canvas RGBA frame with a transparent corner.
            result = next(frame for frame in batch.frames if frame.spec.index == 0)
            composited = result.composited
            assert composited is not None
            self.assertTrue(composited.has_alpha)
            self.assertEqual(composited.image.getpixel((0, 0))[3], 0)
            entry = report["frames"][0]
            self.assertTrue(entry["channel"]["has_alpha_channel"])
            self.assertLess(entry["channel"]["alpha_range"][0], 255)

    def test_every_exported_frame_has_channel_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            report = build_report(build_batch(parse_spec(spec_path)))
            for entry in report["frames"]:
                if entry["usable"]:
                    self.assertIsNotNone(entry["channel"], entry["index"])
                    self.assertIn("has_alpha_channel", entry["channel"])


if __name__ == "__main__":
    unittest.main()
