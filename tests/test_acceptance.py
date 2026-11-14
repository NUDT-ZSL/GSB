"""Acceptance tests for the lightmap seam checker.

Each test maps to one numbered acceptance criterion from the brief.  The
suite uses only the standard library plus Pillow, so it runs with:

    python -m unittest discover -s tests -v

Tests build their data in temporary directories from the deterministic
sample generator; they never touch the checked-in ``samples/`` directory.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lightmap_seams.report import RESULT_FILENAME, run_analysis  # noqa: E402
from lightmap_seams.samples import build_sample_dataset  # noqa: E402

SESSION_SIZE = (512, 512)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def load_result(output_dir: Path) -> dict:
    return json.loads((output_dir / RESULT_FILENAME).read_text(encoding="utf-8"))


def entry_by_image(report: dict, image_label: str) -> dict:
    for entry in report["lightmaps"]:
        if entry["image"] == image_label:
            return entry
    raise AssertionError(f"no report entry for {image_label}")


def colour_counts(png_path: Path) -> tuple[int, int, tuple[int, int]]:
    """Count exact red / blue annotation pixels and return the image size."""

    from PIL import Image

    with Image.open(png_path) as image:
        image = image.convert("RGB")
        size = image.size
        colours = image.getcolors(maxcolors=1_000_000) or []
    red = sum(count for count, colour in colours if colour == (255, 0, 0))
    blue = sum(count for count, colour in colours if colour == (0, 0, 255))
    return red, blue, size


def classify(entry: dict, seam_id: str) -> dict:
    for seam in entry["seams"]:
        if seam["seam_id"] == seam_id:
            return seam
    raise AssertionError(f"no seam {seam_id} in entry {entry['name']}")


class DatasetCase(unittest.TestCase):
    """Shared fixture: a fresh copy of the deterministic sample dataset."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.input_dir = self.base / "input"
        self.output_dir = self.base / "output"
        self.settings = build_sample_dataset(self.input_dir)

    def run_check(self) -> dict:
        return run_analysis(self.settings, self.output_dir)


class SampleContractTests(DatasetCase):
    def test_10_result_json_records_counts_per_lightmap(self) -> None:
        """Criterion 10: parseable JSON with status and all counters."""

        report = self.run_check()

        self.assertEqual(report["threshold"], 32)
        expected = {
            "sample_lightmap_a.png": (5, 1, 1, 1),
            "sample_lightmap_b.png": (3, 0, 0, 0),
            "sample_lightmap_c.png": (2, 0, 0, 0),
        }
        for image_label, (total, leaks, oob, duplicates) in expected.items():
            entry = entry_by_image(report, image_label)
            self.assertEqual(entry["status"], "completed", entry)
            self.assertEqual(entry["total_seams"], total)
            self.assertEqual(entry["leak_count"], leaks)
            self.assertEqual(entry["out_of_bounds_count"], oob)
            self.assertEqual(entry["duplicate_count"], duplicates)
            self.assertEqual(len(entry["seams"]), total)
            self.assertIsNotNone(entry["report_file"])

    def test_02_brightness_step_is_reported_as_leak(self) -> None:
        """Criterion 2: a strong brightness step marks the seam as leaking."""

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        seam = classify(entry, "leak_center")
        self.assertEqual(seam["classification"], "leak")
        self.assertGreater(seam["max_difference"], 32)
        self.assertEqual(entry["leak_count"], 1)

    def test_03_out_of_uv_seam_is_excluded_from_leak_judgement(self) -> None:
        """Criterion 3: u=1.2 is out of bounds and cannot be a leak."""

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        seam = classify(entry, "out_of_bounds")
        self.assertEqual(seam["classification"], "out_of_bounds")
        self.assertIn("越界", seam["reason"])
        self.assertEqual(entry["out_of_bounds_count"], 1)
        # The out-of-bounds seam crosses the same brightness step as the
        # leak seam; only bounds checking keeps it out of the leak count.
        self.assertNotIn("max_difference", seam)

    def test_04_annotation_uses_two_distinguishable_colours(self) -> None:
        """Criterion 4: leak red and out-of-bounds blue both detectable."""

        report = self.run_check()
        png = self.output_dir / entry_by_image(
            report, "sample_lightmap_a.png"
        )["report_file"]
        red, blue, _ = colour_counts(png)
        self.assertGreater(red, 0)
        self.assertGreater(blue, 0)

    def test_09_difference_equal_to_threshold_is_not_a_leak(self) -> None:
        """Criterion 9: boundary rule - equality is NOT a leak."""

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_c.png")
        seam = classify(entry, "c_exact_threshold")
        self.assertEqual(seam["classification"], "ok")
        self.assertAlmostEqual(seam["max_difference"], 32.0, places=6)
        self.assertEqual(entry["leak_count"], 0)

    def test_08_duplicate_endpoint_pair_counted_once(self) -> None:
        """Criterion 8: a copied seam does not change the leak total."""

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        self.assertEqual(entry["duplicate_count"], 1)
        self.assertEqual(entry["total_seams"], 5)  # 6 rows, 1 duplicate
        self.assertEqual(entry["leak_count"], 1)  # not 2
        self.assertEqual(len(entry["duplicates"]), 1)
        self.assertEqual(entry["duplicates"][0]["seam_id"], "leak_center_copy")
        self.assertEqual(entry["duplicates"][0]["duplicate_of"], "leak_center")

    def test_12_identical_endpoints_are_degenerate(self) -> None:
        """Criterion 12: identical endpoints are degenerate, never leaks."""

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        seam = classify(entry, "degenerate_point")
        self.assertEqual(seam["classification"], "degenerate")
        self.assertIn("退化", seam["reason"])
        self.assertEqual(entry["leak_count"], 1)
        leaks = [s for s in entry["seams"] if s["classification"] == "leak"]
        self.assertEqual([s["seam_id"] for s in leaks], ["leak_center"])

    def test_11_annotation_naming_size_and_inputs_untouched(self) -> None:
        """Criterion 11: <stem>_seam_report.png, input bytes unchanged."""

        before = {
            path.name: sha256(path)
            for path in self.input_dir.iterdir()
            if path.is_file()
        }
        report = self.run_check()
        after = {
            path.name: sha256(path)
            for path in self.input_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(before, after)

        done_dir = {path.name for path in self.output_dir.iterdir()}
        for entry in report["lightmaps"]:
            stem = Path(entry["image"]).stem
            self.assertEqual(entry["report_file"], f"{stem}_seam_report.png")
            self.assertIn(entry["report_file"], done_dir)
        self.assertEqual(len([n for n in done_dir if n.endswith("_seam_report.png")]), 3)

    def test_14_summary_matches_detail(self) -> None:
        """Criterion 14: top-level summary equals the per-lightmap sums."""

        report = self.run_check()
        entries = report["lightmaps"]
        summary = report["summary"]
        self.assertEqual(summary["total_lightmaps"], len(entries))
        self.assertEqual(
            summary["completed_lightmaps"],
            sum(1 for entry in entries if entry["status"] == "completed"),
        )
        self.assertEqual(
            summary["incomplete_lightmaps"],
            sum(1 for entry in entries if entry["status"] != "completed"),
        )
        self.assertEqual(
            summary["total_leaks"], sum(entry["leak_count"] for entry in entries)
        )
        self.assertEqual(summary["completed_lightmaps"] + summary["incomplete_lightmaps"],
                         summary["total_lightmaps"])

    def test_15_problem_free_annotation_is_plain_copy(self) -> None:
        """Criterion 15: sized like the input, no palette pixels when clean."""

        report = self.run_check()
        for label in ("sample_lightmap_b.png", "sample_lightmap_c.png"):
            entry = entry_by_image(report, label)
            png = self.output_dir / entry["report_file"]
            red, blue, size = colour_counts(png)
            self.assertEqual(red + blue, 0)
            from PIL import Image

            with Image.open(self.input_dir / label) as source:
                self.assertEqual(size, source.size)
                source_rgb = source.convert("RGB")
                with Image.open(png) as annotated:
                    self.assertEqual(
                        annotated.convert("RGB").tobytes(),
                        source_rgb.tobytes(),
                        "clean annotation must be pixel-identical to input",
                    )
            self.assertEqual(size, SESSION_SIZE)


class PerLightmapFailureTests(DatasetCase):
    def test_01_corrupt_lightmap_does_not_stop_the_others(self) -> None:
        """Criterion 1: one corrupt PNG, the remaining lightmaps still pass."""

        (self.input_dir / "sample_lightmap_a.png").write_bytes(
            b"\x89PNG\r\n\x1a\nthis is not a valid image body\x00" * 4
        )
        report = self.run_check()

        broken = entry_by_image(report, "sample_lightmap_a.png")
        self.assertEqual(broken["status"], "incomplete")
        self.assertIn("无法解码", broken["reason"])
        for label in ("sample_lightmap_b.png", "sample_lightmap_c.png"):
            entry = entry_by_image(report, label)
            self.assertEqual(entry["status"], "completed")
            self.assertTrue((self.output_dir / entry["report_file"]).is_file())

    def test_05_missing_endpoint_columns_marks_incomplete(self) -> None:
        """Criterion 5a: removing the u2/v2 columns is a format problem."""

        csv_path = self.input_dir / "sample_lightmap_a_seams.csv"
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        header = "seam_id,u1,v1"
        rewritten = [header]
        for line in lines[1:]:
            cells = line.split(",")
            rewritten.append(",".join(cells[:3]))
        csv_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("列缺失", entry["reason"])
        self.assertIn("u2", entry["reason"])
        for label in ("sample_lightmap_b.png", "sample_lightmap_c.png"):
            self.assertEqual(entry_by_image(report, label)["status"], "completed")

    def test_05b_short_row_marks_incomplete(self) -> None:
        """Criterion 5b: a row with fewer fields than the header fails."""

        csv_path = self.input_dir / "sample_lightmap_a_seams.csv"
        text = csv_path.read_text(encoding="utf-8")
        text += "truncated_row,0.2,0.4\n"
        csv_path.write_text(text, encoding="utf-8")

        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_a.png")
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("字段数不足", entry["reason"])

    def test_06_missing_png_means_no_annotation(self) -> None:
        """Criterion 6: a missing PNG yields no annotation and a clear reason."""

        (self.input_dir / "sample_lightmap_b.png").unlink()
        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_b.png")
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("光照贴图缺失", entry["reason"])
        self.assertIsNone(entry["report_file"])
        self.assertFalse((self.output_dir / "sample_lightmap_b_seam_report.png").exists())

    def test_06b_stale_annotation_from_previous_run_is_removed(self) -> None:
        """Criterion 6: a prior annotation must not survive a failing run."""

        self.run_check()  # good run creates all three reports
        stale = self.output_dir / "sample_lightmap_b_seam_report.png"
        self.assertTrue(stale.is_file())

        (self.input_dir / "sample_lightmap_b.png").unlink()
        self.run_check()
        self.assertFalse(stale.exists())

    def test_13_missing_seam_csv_marks_incomplete(self) -> None:
        """Criterion 13: a missing seam list leaves the remaining ones intact."""

        (self.input_dir / "sample_lightmap_c_seams.csv").unlink()
        report = self.run_check()
        entry = entry_by_image(report, "sample_lightmap_c.png")
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("接缝清单", entry["reason"])
        self.assertIsNone(entry["report_file"])
        for label in ("sample_lightmap_a.png", "sample_lightmap_b.png"):
            self.assertEqual(entry_by_image(report, label)["status"], "completed")


class SettingsFailureTests(DatasetCase):
    def _rewrite_settings(self, **changes) -> None:
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings.update(changes)
        self.settings.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _assert_whole_report_incomplete(self, report: dict) -> None:
        self.assertEqual(report["status"], "incomplete")
        self.assertIsNotNone(report["reason"])
        self.assertEqual(report["summary"]["completed_lightmaps"], 0)
        self.assertEqual(
            report["summary"]["incomplete_lightmaps"],
            report["summary"]["total_lightmaps"],
        )

    def test_07_threshold_out_of_range_aborts_report(self) -> None:
        """Criterion 7a: threshold -1 marks the whole report incomplete."""

        self._rewrite_settings(threshold=-1)
        report = self.run_check()
        self._assert_whole_report_incomplete(report)
        self.assertIn("阈值无效", report["reason"])
        self.assertEqual(report["summary"]["total_lightmaps"], 3)

    def test_07b_non_numeric_threshold_aborts_report(self) -> None:
        """Criterion 7b: a string/none threshold is rejected too."""

        for bad_value in ("32", 32.5, None, True, 256):
            with self.subTest(threshold=bad_value):
                self._rewrite_settings(threshold=bad_value)
                report = self.run_check()
                self._assert_whole_report_incomplete(report)
                self.assertIn("阈值无效", report["reason"])

    def test_07c_invalid_threshold_removes_stale_annotations(self) -> None:
        """Criterion 7: a failed run leaves no annotation behind."""

        self.run_check()
        self.assertEqual(
            len(list(self.output_dir.glob("*_seam_report.png"))), 3
        )
        self._rewrite_settings(threshold=300)
        self.run_check()
        self.assertEqual(list(self.output_dir.glob("*_seam_report.png")), [])

    def test_settings_missing_lightmap_field_marks_entry_incomplete(self) -> None:
        """Robustness: a malformed lightmap entry does not stop the run."""

        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        del settings["lightmaps"][1]["image"]
        self.settings.write_text(json.dumps(settings), encoding="utf-8")
        report = self.run_check()
        entry = report["lightmaps"][1]
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("image", entry["reason"])
        self.assertNotEqual(report["lightmaps"][0]["status"], "incomplete")
        self.assertNotEqual(report["lightmaps"][2]["status"], "incomplete")


class SamplePointBoundsTests(unittest.TestCase):
    """Criterion 3 covers sample points, not only the declared endpoints."""

    def setUp(self) -> None:
        from PIL import Image

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.input_dir.mkdir(parents=True)
        Image.new("RGB", (64, 64), (40, 40, 40)).save(
            self.input_dir / "edge_lightmap.png"
        )
        (self.input_dir / "edge_lightmap_seams.csv").write_text(
            "seam_id,u1,v1,u2,v2\n"
            # Both endpoints inside UV[0,1] and inside the image, but the
            # +2 px side sample crosses the top edge.
            "near_top_edge,0.2,0.995,0.8,0.995\n"
            "negative_uv,-0.1,0.5,0.5,0.5\n"
            "clean_middle,0.2,0.5,0.8,0.5\n",
            encoding="utf-8",
        )
        self.settings = self.input_dir / "edge_settings.json"
        self.settings.write_text(
            json.dumps(
                {
                    "threshold": 10,
                    "lightmaps": [
                        {
                            "image": "edge_lightmap.png",
                            "seams_csv": "edge_lightmap_seams.csv",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_sample_point_outside_image_is_out_of_bounds(self) -> None:
        report = run_analysis(self.settings, self.output_dir)
        entry = report["lightmaps"][0]
        near_edge = classify(entry, "near_top_edge")
        self.assertEqual(near_edge["classification"], "out_of_bounds")
        self.assertIn("采样点", near_edge["reason"])
        self.assertEqual(entry["out_of_bounds_count"], 2)

        negative = classify(entry, "negative_uv")
        self.assertEqual(negative["classification"], "out_of_bounds")
        self.assertIn("UV[0,1]", negative["reason"])

        clean = classify(entry, "clean_middle")
        self.assertEqual(clean["classification"], "ok")

        png = self.output_dir / entry["report_file"]
        red, blue, size = colour_counts(png)
        self.assertEqual(red, 0)
        self.assertGreater(blue, 0)
        self.assertEqual(size, (64, 64))


class InputEdgeCaseTests(unittest.TestCase):
    """Inputs outside the happy path must produce a report, not a crash."""

    def setUp(self) -> None:
        from PIL import Image

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.input_dir.mkdir(parents=True)
        Image.new("RGB", (4, 4), (20, 20, 20)).save(self.input_dir / "tiny.png")

    def test_non_finite_coordinate_is_a_format_problem(self) -> None:
        (self.input_dir / "bad.csv").write_text(
            "u1,v1,u2,v2\n0.1,0.1,0.2,0.2\nnan,0.3,0.4,0.5\n",
            encoding="utf-8",
        )
        settings = self.input_dir / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "threshold": 8,
                    "lightmaps": [
                        {"image": "tiny.png", "seams_csv": "bad.csv"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = run_analysis(settings, self.output_dir)
        entry = report["lightmaps"][0]
        self.assertEqual(entry["status"], "incomplete")
        self.assertIn("不是有限数值", entry["reason"])

    def test_unicode_bom_semicolon_csv_is_accepted(self) -> None:
        (self.input_dir / "eu.csv").write_text(
            "﻿u_a;v_a;u_b;v_b\n0.1;0.1;0.9;0.1\n", encoding="utf-8"
        )
        settings = self.input_dir / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "threshold": 8,
                    "lightmaps": [
                        {"image": "tiny.png", "seams_csv": "eu.csv"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        report = run_analysis(settings, self.output_dir)
        entry = report["lightmaps"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["total_seams"], 1)
        self.assertIn("_seam_report.png", entry["report_file"])


class DeterminismTests(unittest.TestCase):
    def test_generator_is_byte_identical_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            build_sample_dataset(Path(first))
            build_sample_dataset(Path(second))
            for path in sorted(Path(first).iterdir()):
                self.assertEqual(
                    sha256(path), sha256(Path(second) / path.name), path.name
                )

    def test_report_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            settings = build_sample_dataset(tmp_path / "input")
            run_analysis(settings, tmp_path / "out1")
            run_analysis(settings, tmp_path / "out2")
            first = (tmp_path / "out1" / RESULT_FILENAME).read_text(encoding="utf-8")
            second = (tmp_path / "out2" / RESULT_FILENAME).read_text(encoding="utf-8")
            # Paths differ between runs; the analysis payload must not.
            self.assertEqual(
                json.loads(first)["lightmaps"], json.loads(second)["lightmaps"]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
