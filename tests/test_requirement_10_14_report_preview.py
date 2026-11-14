"""Requirements 10-12, 14: report schema, preview page, failure messages, repeatability."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from frame_export.batch import build_batch
from frame_export.report import build_preview_html, build_report
from frame_export.spec import parse_spec
from tests.helpers import ROOT, copy_sample, load_json

REQUIRED_FRAME_FIELDS = {
    "index",
    "ordinal",
    "source_file",
    "duration_ms",
    "status",
    "usable",
    "size_processing",
    "channel",
    "output",
    "sha256",
}


class ReportSchemaTests(unittest.TestCase):
    def test_every_frame_entry_has_required_fields_in_description_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            described = [frame["index"] for frame in load_json(spec_path)["frames"]]
            report = build_report(build_batch(parse_spec(spec_path)))

            self.assertEqual([entry["index"] for entry in report["frames"]], described)
            for entry in report["frames"]:
                missing = REQUIRED_FRAME_FIELDS - set(entry)
                self.assertFalse(missing, (entry["ordinal"], missing))
                self.assertEqual(entry["ordinal"], described.index(entry["index"]) + 1)
                for key in ("source_size", "output_size", "placement", "scale"):
                    self.assertIn(key, entry["size_processing"])
                for key in ("source_mode", "has_alpha_channel", "alpha_range"):
                    self.assertIn(key, entry["channel"])
                # Source file is recorded exactly as the description wrote it.
                self.assertIn("frames/", entry["source_file"])

    def test_report_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            report = build_report(build_batch(parse_spec(spec_path)))
            self.assertEqual(report["canvas"], {"width": 320, "height": 180})
            self.assertEqual(report["background"]["value"], "#101820")
            self.assertEqual(report["fps"], 24)
            self.assertIn(report["verdict"], ("complete", "partial", "unexportable"))
            self.assertIn("verdict_reason", report)
            self.assertEqual(report["generator"]["name"], "frame_export")
            self.assertNotIn("generated_at", report["generator"])

    def test_report_json_is_valid_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            _run_cli(ROOT / "samples" / "clean_storyboard" / "description.json", out)
            raw = (out / "report.json").read_bytes()
            raw.decode("utf-8")
            self.assertTrue(raw.endswith(b"\n"))
            parsed = json.loads(raw)
            self.assertEqual(len(parsed["frames"]), 18)


class PreviewTests(unittest.TestCase):
    def test_preview_lists_all_exported_frames_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            html = build_preview_html(batch, build_report(batch))
            shown = [int(v) for v in re.findall(r'data-ordinal="(\d+)"', html)]
            self.assertEqual(shown, list(range(1, 19)))
            indexes = re.findall(r'data-index="(\d+)"', html)
            self.assertEqual([int(v) for v in indexes], list(range(18)))
            images = re.findall(r'<img class="thumb" src="([^"]+)"', html)
            self.assertEqual(
                images, [frame.png_relpath for frame in batch.succeeded]
            )
            self.assertEqual(len(images), 18)

    def test_every_preview_src_resolves_to_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            _run_cli(ROOT / "samples" / "clean_storyboard" / "description.json", out)
            html = (out / "preview.html").read_text(encoding="utf-8")
            sources = re.findall(r'<img class="thumb" src="([^"]+)"', html)
            self.assertTrue(sources)
            for relative in sources:
                target = (out / relative).resolve()
                self.assertTrue(target.is_file(), relative)
            # No double extensions or dangling links.
            self.assertNotIn(".png.png", html)

    def test_preview_has_no_network_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            html = build_preview_html(batch, build_report(batch))
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)
            self.assertNotIn("<script src=", html)
            self.assertNotIn("<link ", html)

    def test_preview_marks_failed_frames_without_fake_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "storyboard_issues")
            batch = build_batch(parse_spec(spec_path))
            html = build_preview_html(batch, build_report(batch))
            self.assertIn('class="frame-card failed"', html)
            self.assertEqual(html.count('class="missing"'), 4)
            self.assertIn("文件缺失", html)
            self.assertIn("无法解码", html)
            self.assertIn("参数无效", html)

    def test_preview_page_is_usable_standalone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            html = build_preview_html(batch, build_report(batch))
            self.assertTrue(html.startswith("<!DOCTYPE html>"))
            self.assertIn('<meta charset="utf-8">', html)
            self.assertIn("播放", html)
            self.assertIn("data-duration-ms", html)


class InvalidConfigTests(unittest.TestCase):
    def test_fps_zero_returns_readable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            completed = _run_cli(ROOT / "samples" / "invalid_fps" / "description.json", out)
            self.assertEqual(completed.returncode, 3, completed.stderr)
            self.assertIn("帧率", completed.stderr)
            report = load_json(out / "report.json")
            self.assertEqual(report["verdict"], "unexportable")
            self.assertEqual(report["error"]["code"], "invalid_fps")
            self.assertTrue((out / "preview.html").is_file())
            preview = (out / "preview.html").read_text(encoding="utf-8")
            self.assertIn("无法导出", preview)

    def test_invalid_canvas_returns_readable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "bad.json"
            spec.write_text(
                json.dumps(
                    {
                        "canvas": {"width": 0, "height": 180},
                        "background": "#101820",
                        "fps": 24,
                        "frames": [],
                    }
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "out"
            completed = _run_cli(spec, out)
            self.assertEqual(completed.returncode, 3)
            report = load_json(out / "report.json")
            self.assertEqual(report["error"]["code"], "invalid_canvas")
            self.assertIn("画幅", report["error"]["message"])

    def test_malformed_json_is_unexportable_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "broken.json"
            spec.write_text('{"canvas": {"width": 10,', encoding="utf-8")
            out = Path(tmp) / "out"
            completed = _run_cli(spec, out)
            self.assertEqual(completed.returncode, 3)
            report = load_json(out / "report.json")
            self.assertEqual(report["error"]["code"], "spec_malformed")
            self.assertIn("JSON", report["error"]["message"])

    def test_missing_spec_is_unexportable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            completed = _run_cli(Path(tmp) / "nope.json", out)
            self.assertEqual(completed.returncode, 3)
            report = load_json(out / "report.json")
            self.assertEqual(report["error"]["code"], "spec_missing")


class OutputPackageTests(unittest.TestCase):
    def test_manifest_lists_exactly_the_written_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            _run_cli(ROOT / "samples" / "storyboard_issues" / "description.json", out)
            manifest = load_json(out / ".frame_export_manifest.json")
            listed = set(manifest["files"])
            actual = {
                path.relative_to(out).as_posix()
                for path in out.rglob("*")
                if path.is_file() and path.name != ".frame_export_manifest.json"
            }
            self.assertEqual(listed, actual)
            self.assertEqual(manifest["verdict"], "partial")

    def test_rerun_removes_stale_frames_but_keeps_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            _run_cli(ROOT / "samples" / "clean_storyboard" / "description.json", out)
            self.assertEqual(len(list((out / "frames").iterdir())), 18)
            (out / "notes-from-artist.txt").write_text("keep me", encoding="utf-8")

            _run_cli(ROOT / "samples" / "storyboard_issues" / "description.json", out)
            self.assertEqual(len(list((out / "frames").iterdir())), 14)
            self.assertEqual(
                (out / "notes-from-artist.txt").read_text(encoding="utf-8"),
                "keep me",
            )
            self.assertFalse((out / "frames" / "frame_0007_index_0006.png").exists())


class RepeatabilityTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = ROOT / "samples" / "clean_storyboard" / "description.json"
            first = Path(tmp) / "run1"
            second = Path(tmp) / "run2"
            _run_cli(spec, first)
            _run_cli(spec, second)

            for name in ("report.json", "preview.html"):
                self.assertEqual(
                    (first / name).read_bytes(),
                    (second / name).read_bytes(),
                    name,
                )
            first_frames = sorted((first / "frames").iterdir())
            second_frames = sorted((second / "frames").iterdir())
            self.assertEqual(
                [path.name for path in first_frames],
                [path.name for path in second_frames],
            )
            for left, right in zip(first_frames, second_frames):
                self.assertEqual(left.read_bytes(), right.read_bytes(), left.name)

    def test_rerun_into_same_directory_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = ROOT / "samples" / "clean_storyboard" / "description.json"
            out = Path(tmp) / "out"
            _run_cli(spec, out)
            first = (out / "report.json").read_bytes()
            first_preview = (out / "preview.html").read_bytes()
            _run_cli(spec, out)
            self.assertEqual(first, (out / "report.json").read_bytes())
            self.assertEqual(first_preview, (out / "preview.html").read_bytes())

    def test_cli_refuses_foreign_non_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "foreign"
            out.mkdir()
            (out / "keep.txt").write_text("do not delete", encoding="utf-8")
            completed = _run_cli(
                ROOT / "samples" / "clean_storyboard" / "description.json", out
            )
            self.assertEqual(completed.returncode, 1)
            self.assertTrue((out / "keep.txt").exists())


def _run_cli(spec: Path, out: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "frame_export", str(spec), str(out)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


if __name__ == "__main__":
    unittest.main()
