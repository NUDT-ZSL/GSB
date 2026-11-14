"""Requirement 5: per-frame duration drives the preview timeline and its total."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.report import build_preview_html, build_report
from frame_export.spec import parse_spec
from tests.helpers import copy_sample, load_json


class TimelineTests(unittest.TestCase):
    def test_total_duration_matches_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            described = load_json(spec_path)["frames"]
            expected = sum(frame["duration_ms"] for frame in described)

            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            self.assertEqual(report["summary"]["duration_ms_exported"], expected)
            self.assertEqual(batch.total_duration_ms, expected)

            html = build_preview_html(batch, report)
            self.assertIn(f"累计 {expected} ms", html)
            # Each card exposes its own duration to the player script.
            data_durations = re.findall(r'data-duration-ms="([^"]+)"', html)
            self.assertEqual(
                [float(value) for value in data_durations],
                [float(frame["duration_ms"]) for frame in described],
            )

    def test_timeline_segments_are_proportional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            html = build_preview_html(batch, report)
            widths = [float(value) for value in re.findall(r"width:([0-9.]+)%", html)]
            self.assertEqual(len(widths), len(batch.succeeded))
            total = report["summary"]["duration_ms_exported"]
            durations = [float(frame.spec.duration_ms) for frame in batch.succeeded]
            for width, duration in zip(widths, durations):
                self.assertAlmostEqual(width, duration / total * 100, places=3)

    def test_varied_durations_produce_varied_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            report = build_report(build_batch(parse_spec(spec_path)))
            durations = [frame["duration_ms"] for frame in report["frames"]]
            self.assertGreater(len(set(durations)), 5)


if __name__ == "__main__":
    unittest.main()
