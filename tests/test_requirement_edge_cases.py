"""Cross-cutting rules: sequence-only verdicts, safe paths, no output collisions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from frame_export.batch import build_batch
from frame_export.report import build_report
from frame_export.spec import parse_spec
from tests.helpers import load_json, png_bytes, write_description


def _solid(directory: Path, name: str, color: tuple[int, int, int]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(directory / name)


class SequenceOnlyVerdictTests(unittest.TestCase):
    def test_all_frames_exported_but_index_gap_still_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/a.png", "duration_ms": 40},
                    {"index": 2, "file": "frames/a.png", "duration_ms": 40},
                ],
                frame_count=3,
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["summary"]["frames_exported"], 2)
            self.assertEqual(report["verdict"], "partial")
            self.assertIn("缺失序号 1", report["verdict_reason"])
            self.assertEqual(report["sequence"]["missing_indices"], [1])

    def test_duplicate_index_still_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/a.png", "duration_ms": 40},
                    {"index": 0, "file": "frames/a.png", "duration_ms": 40},
                ],
                frame_count=1,
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "partial")
            self.assertEqual(report["sequence"]["duplicate_indices"], [0])
            self.assertIn("重复序号 0", report["verdict_reason"])

    def test_declared_count_mismatch_still_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            spec_path = write_description(
                Path(tmp),
                [{"index": 0, "file": "frames/a.png", "duration_ms": 40}],
                frame_count=4,
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "partial")
            self.assertFalse(report["sequence"]["declared_count_matches"])
            self.assertEqual(report["sequence"]["missing_indices"], [1, 2, 3])


class OutputCollisionTests(unittest.TestCase):
    def test_duplicate_indices_write_distinct_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp) / "frames"
            _solid(frames, "red.png", (200, 0, 0))
            _solid(frames, "green.png", (0, 200, 0))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 4, "file": "frames/red.png", "duration_ms": 10},
                    {"index": 4, "file": "frames/green.png", "duration_ms": 10},
                ],
            )
            batch = build_batch(parse_spec(spec_path))
            paths = [frame.png_relpath for frame in batch.succeeded]
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(set(paths)), 2)
            self.assertNotEqual(
                batch.succeeded[0].png_bytes, batch.succeeded[1].png_bytes
            )


class SafePathTests(unittest.TestCase):
    def test_path_traversal_is_invalid_and_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.png"
            outside.write_bytes(png_bytes(Image.new("RGB", (10, 10), (9, 9, 9))))
            work = Path(tmp) / "work"
            work.mkdir()
            spec_path = write_description(
                work,
                [
                    {"index": 0, "file": "../secret.png", "duration_ms": 10},
                    {"index": 1, "file": "C:/Windows/win.ini", "duration_ms": 10},
                ],
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "unexportable")
            for entry in report["frames"]:
                self.assertEqual(entry["error"]["code"], "invalid_file_path")
                self.assertIsNone(entry["output"])

    def test_absolute_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "abs.png"
            image.write_bytes(png_bytes(Image.new("RGB", (10, 10), (1, 1, 1))))
            spec_path = write_description(
                Path(tmp),
                [{"index": 0, "file": str(image).replace("\\", "/"), "duration_ms": 10}],
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["frames"][0]["error"]["code"], "invalid_file_path")


class DurationPrecedenceTests(unittest.TestCase):
    def test_invalid_duration_reported_before_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = write_description(
                Path(tmp),
                [{"index": 0, "file": "frames/nope.png", "duration_ms": -5}],
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["frames"][0]["status"], "invalid")
            self.assertEqual(report["frames"][0]["error"]["code"], "invalid_duration")


class MetadataEdgeTests(unittest.TestCase):
    def test_one_based_indices_do_not_report_index_zero_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 1, "file": "frames/a.png", "duration_ms": 40},
                    {"index": 2, "file": "frames/a.png", "duration_ms": 40},
                    {"index": 3, "file": "frames/a.png", "duration_ms": 40},
                ],
                frame_count=3,
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["sequence"]["missing_indices"], [])
            self.assertEqual(report["sequence"]["index_base"], 1)
            self.assertEqual(report["verdict"], "complete")

    def test_zero_based_indices_stay_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/a.png", "duration_ms": 40},
                    {"index": 1, "file": "frames/a.png", "duration_ms": 40},
                ],
                frame_count=2,
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["sequence"]["missing_indices"], [])
            self.assertEqual(report["sequence"]["index_base"], 0)
            self.assertEqual(report["verdict"], "complete")

    def test_missing_background_uses_default_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            payload = {
                "canvas": [64, 64],
                "fps": 24,
                "frames": [{"index": 0, "file": "frames/a.png", "duration_ms": 10}],
            }
            path = Path(tmp) / "description.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = build_report(build_batch(parse_spec(path)))
            self.assertEqual(report["background"]["value"], "#000000")
            self.assertTrue(any("默认值" in note for note in report["notes"]))

    def test_aliases_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _solid(Path(tmp) / "frames", "a.png", (10, 20, 30))
            payload = {
                "size": {"w": 32, "h": 32},
                "backgroundColor": "#FFFFFF",
                "frame_rate": 30,
                "frames": [{"seq": 0, "filename": "frames/a.png", "hold_ms": 25}],
            }
            path = Path(tmp) / "description.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = build_report(build_batch(parse_spec(path)))
            self.assertEqual(report["canvas"], {"width": 32, "height": 32})
            self.assertEqual(report["fps"], 30)
            self.assertEqual(report["frames"][0]["duration_ms"], 25)
            self.assertEqual(report["verdict"], "complete")


if __name__ == "__main__":
    unittest.main()
