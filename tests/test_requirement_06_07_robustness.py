"""Requirement 6 & 7: bad durations invalidate one frame; corrupt files do not stop the batch."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from frame_export.batch import build_batch
from frame_export.report import build_preview_html, build_report
from frame_export.spec import parse_spec
from tests.helpers import copy_sample, load_json, png_bytes, write_description


class InvalidDurationTests(unittest.TestCase):
    def test_negative_duration_marks_frame_invalid_but_not_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            payload = load_json(spec_path)
            payload["frames"][2]["duration_ms"] = -1
            spec_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)

            failed = [frame for frame in batch.frames if not frame.succeeded]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].spec.index, 2)
            self.assertEqual(failed[0].error_code, "invalid_duration")
            self.assertEqual(report["verdict"], "partial")
            self.assertEqual(report["summary"]["frames_exported"], 17)
            # The remaining frames are untouched by the invalid one.
            self.assertEqual(
                [frame.spec.index for frame in batch.succeeded],
                [index for index in range(18) if index != 2],
            )
            for frame in batch.failed:
                self.assertIsNone(frame.png_bytes)

    def test_non_numeric_duration_marks_frame_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            image = Image.new("RGB", (32, 32), (10, 20, 30))
            (frames_dir / "a.png").write_bytes(png_bytes(image))
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/a.png", "duration_ms": 100},
                    {"index": 1, "file": "frames/a.png", "duration_ms": "abc"},
                    {"index": 2, "file": "frames/a.png", "duration_ms": 200},
                ],
            )
            batch = build_batch(parse_spec(spec_path))
            self.assertEqual(batch.verdict, "partial")
            self.assertEqual(batch.succeeded[0].spec.index, 0)
            self.assertEqual(batch.succeeded[1].spec.index, 2)
            self.assertEqual(batch.failed[0].error_code, "invalid_duration")

    def test_bool_duration_is_not_accepted_as_a_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            Image.new("RGB", (8, 8), (0, 0, 0)).save(frames_dir / "a.png")
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/a.png", "duration_ms": True},
                    {"index": 1, "file": "frames/a.png", "duration_ms": 50},
                ],
            )
            batch = build_batch(parse_spec(spec_path))
            self.assertEqual(batch.frames[0].status, "invalid")
            self.assertEqual(batch.frames[1].status, "ok")


class CorruptFrameTests(unittest.TestCase):
    def test_corrupt_png_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "storyboard_issues")
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            corrupt = [
                frame for frame in batch.frames if frame.spec.index == 9
            ]
            self.assertEqual(len(corrupt), 1)
            self.assertEqual(corrupt[0].status, "corrupt")
            self.assertEqual(corrupt[0].error_code, "frame_undecodable")
            self.assertGreaterEqual(len(batch.succeeded), 10)
            self.assertEqual(report["verdict"], "partial")
            html = build_preview_html(batch, report)
            self.assertIn("未完成", html)
            # A failed frame must not be rendered as an image at all.
            referenced = html.split('class="thumb"')
            self.assertEqual(len(referenced) - 1, len(batch.succeeded))
            for frame in batch.failed:
                self.assertIsNone(frame.png_relpath)
            self.assertNotIn(corrupt[0], batch.succeeded)

    def test_truncated_and_garbage_files_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            good = Image.new("RGB", (64, 64), (200, 40, 40))
            (frames_dir / "good.png").write_bytes(png_bytes(good))
            full = png_bytes(Image.new("RGB", (64, 64), (40, 200, 40)))
            (frames_dir / "truncated.png").write_bytes(full[: len(full) // 2])
            (frames_dir / "garbage.png").write_bytes(b"not a png at all")
            (frames_dir / "empty.png").write_bytes(b"")
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/truncated.png", "duration_ms": 10},
                    {"index": 1, "file": "frames/good.png", "duration_ms": 10},
                    {"index": 2, "file": "frames/garbage.png", "duration_ms": 10},
                    {"index": 3, "file": "frames/empty.png", "duration_ms": 10},
                ],
            )
            batch = build_batch(parse_spec(spec_path))
            statuses = {frame.spec.index: frame.status for frame in batch.frames}
            self.assertEqual(statuses[1], "ok")
            self.assertEqual(statuses[0], "corrupt")
            self.assertEqual(statuses[2], "corrupt")
            self.assertEqual(statuses[3], "corrupt")
            self.assertEqual(len(batch.succeeded), 1)


if __name__ == "__main__":
    unittest.main()
