"""Requirements 8, 9, 13: missing files, sequence anomalies, and batch verdict."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.report import build_report
from frame_export.spec import parse_spec
from tests.helpers import copy_sample, png_bytes, write_description
from PIL import Image


class MissingFileTests(unittest.TestCase):
    def test_missing_file_is_reported_and_others_keep_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            baseline = build_batch(parse_spec(spec_path))
            baseline_pixels = {
                frame.spec.index: frame.png_bytes for frame in baseline.succeeded
            }

            victim = spec_path.parent / "frames" / "harbor.png"
            victim.unlink()
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)

            missing = [frame for frame in batch.frames if frame.status == "missing_file"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0].spec.index, 6)
            self.assertEqual(report["verdict"], "partial")

            # Every remaining frame's composite is byte-identical to the
            # baseline: a missing neighbour does not pollute other frames.
            for frame in batch.succeeded:
                self.assertEqual(
                    frame.png_bytes,
                    baseline_pixels[frame.spec.index],
                    frame.spec.index,
                )

    def test_missing_entry_in_report_distinguishes_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "storyboard_issues")
            report = build_report(build_batch(parse_spec(spec_path)))
            statuses = {
                entry["index"]: entry["status"] for entry in report["frames"]
            }
            self.assertEqual(statuses[6], "missing_file")
            self.assertEqual(statuses[9], "corrupt")
            self.assertEqual(statuses[5], "invalid")
            self.assertEqual(statuses[0], "ok")
            exported = {
                entry["index"] for entry in report["frames"] if entry["usable"]
            }
            for index in (5, 6, 9, 13):
                self.assertNotIn(index, exported)


class SequenceTests(unittest.TestCase):
    def test_missing_and_duplicate_indices_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "storyboard_issues")
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["sequence"]["missing_indices"], [3])
            self.assertEqual(report["sequence"]["duplicate_indices"], [4])
            self.assertTrue(
                any("缺失帧序号：3" in note for note in report["notes"]),
                report["notes"],
            )
            self.assertTrue(
                any("重复帧序号：4" in note for note in report["notes"]),
                report["notes"],
            )

    def test_missing_index_is_not_exported_as_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            Image.new("RGB", (40, 40), (1, 2, 3)).save(frames_dir / "a.png")
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 5, "file": "frames/a.png", "duration_ms": 10},
                    {"index": 8, "file": "frames/a.png", "duration_ms": 10},
                ],
                frame_count=10,
            )
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            # 0..9 is the declared range; 5 and 8 exist, the rest are missing.
            self.assertEqual(report["sequence"]["missing_indices"], [0, 1, 2, 3, 4, 6, 7, 9])
            # Only two composites were produced; no placeholder for the gaps.
            self.assertEqual(len(batch.frames), 2)
            self.assertEqual(report["summary"]["frames_total"], 2)
            for entry in report["frames"]:
                self.assertNotIn(entry["index"], (0, 1, 2, 3, 4, 6, 7, 9))
            # A gap must never be represented as an exported blank frame.
            self.assertEqual(
                [entry["index"] for entry in report["frames"] if entry["usable"]],
                [5, 8],
            )

    def test_out_of_order_indices_are_flagged_but_export_uses_array_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            for name, color in (("a.png", (200, 0, 0)), ("b.png", (0, 200, 0))):
                Image.new("RGB", (40, 40), color).save(frames_dir / name)
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 2, "file": "frames/b.png", "duration_ms": 10},
                    {"index": 0, "file": "frames/a.png", "duration_ms": 20},
                ],
                frame_count=3,
            )
            batch = build_batch(parse_spec(spec_path))
            report = build_report(batch)
            self.assertTrue(report["sequence"]["out_of_order"])
            self.assertEqual([f.spec.index for f in batch.frames], [2, 0])
            self.assertEqual(report["summary"]["frames_exported"], 2)
            self.assertEqual(report["sequence"]["missing_indices"], [1])


class VerdictTests(unittest.TestCase):
    def test_all_good_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "complete")

    def test_some_bad_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "storyboard_issues")
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "partial")

    def test_nothing_exported_is_unexportable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "frames"
            frames_dir.mkdir()
            (frames_dir / "bad.png").write_bytes(b"broken")
            spec_path = write_description(
                Path(tmp),
                [
                    {"index": 0, "file": "frames/bad.png", "duration_ms": 10},
                    {"index": 1, "file": "frames/gone.png", "duration_ms": 10},
                ],
            )
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "unexportable")

    def test_empty_description_is_unexportable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = write_description(Path(tmp), [])
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["verdict"], "unexportable")
            self.assertEqual(report["summary"]["frames_total"], 0)


if __name__ == "__main__":
    unittest.main()
