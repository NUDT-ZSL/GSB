"""Verification suite for the sprite packer, mapped one-to-one onto the
nine stated requirements. Run from the project root:

    python -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the project root importable regardless of cwd / isolated mode.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

from sprite_packer import (  # noqa: E402
    EXIT_NO_VALID_FRAMES,
    EXIT_OK,
    EXIT_SEQUENCE_ERROR,
    SequenceError,
    load_rgba,
    pack,
)

GARBAGE = b"THIS_IS_NOT_A_VALID_PNG_FILE" * 4


def write_rgb(path: Path, size=(4, 4), color=(200, 30, 30)) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def write_rgba(path: Path, size=(4, 4), alpha_pattern=(255, 128, 0, 64)) -> None:
    image = Image.new("RGBA", size, (10, 120, 200, 255))
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            r, g, b, _ = pixels[x, y]
            pixels[x, y] = (r, g, b, alpha_pattern[(x + y) % len(alpha_pattern)])
    image.save(path, format="PNG")


def write_garbage(path: Path) -> None:
    path.write_bytes(GARBAGE)


def write_truncated(path: Path) -> None:
    """A PNG with a valid signature/header whose image data is cut short."""
    tmp = path.with_suffix(".tmp.png")
    Image.new("RGB", (8, 8), (1, 2, 3)).save(tmp, format="PNG")
    data = tmp.read_bytes()
    tmp.unlink()
    path.write_bytes(data[: max(8, len(data) * 3 // 5)])


def read_report(report_path: Path) -> dict:
    with open(report_path, encoding="utf-8") as handle:
        return json.load(handle)


class SpritePackerTests(unittest.TestCase):
    def setUp(self) -> None:
        # ignore_cleanup_errors guards against Windows file locking; cleanup
        # ordering (LIFO) still closes every image before the directory is removed.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.in_dir = self.tmp / "frames"
        self.in_dir.mkdir()
        self.out_dir = self.tmp / "out"

    # ---- Requirement 1: duplicate / missing indices abort, no outputs ----
    def test_duplicate_index_aborts_without_outputs(self):
        write_rgb(self.in_dir / "frame_01.png")
        write_rgb(self.in_dir / "frame_02.png")
        write_rgb(self.in_dir / "frame_02_copy.png")
        # Pre-existing stale products must not survive a failed validation.
        self.out_dir.mkdir()
        (self.out_dir / "sprite_sheet.png").write_bytes(b"stale")
        (self.out_dir / "report.json").write_text("{}")

        with self.assertRaises(SequenceError) as ctx:
            pack(self.in_dir, self.out_dir)
        self.assertIn("duplicate", str(ctx.exception))
        self.assertFalse((self.out_dir / "sprite_sheet.png").exists())
        self.assertFalse((self.out_dir / "report.json").exists())

    def test_missing_index_aborts_without_outputs(self):
        write_rgb(self.in_dir / "frame_01.png")
        write_rgb(self.in_dir / "frame_04.png")  # gap at 2, 3
        with self.assertRaises(SequenceError) as ctx:
            pack(self.in_dir, self.out_dir)
        self.assertIn("missing", str(ctx.exception))
        self.assertFalse((self.out_dir / "sprite_sheet.png").exists())
        self.assertFalse((self.out_dir / "report.json").exists())

    def test_unparseable_and_no_png_abort(self):
        write_rgb(self.in_dir / "frame_01.png")
        write_rgb(self.in_dir / "frame_02_extra_9.png")  # two integers
        with self.assertRaises(SequenceError):
            pack(self.in_dir, self.out_dir)

        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(SequenceError):
            pack(empty, self.tmp / "empty_out")

    # ---- Requirement 2: RGBA normalization / alpha handling ----
    def test_rgb_becomes_opaque_rgba_alpha_preserved(self):
        write_rgb(self.in_dir / "frame_1.png", color=(200, 30, 30))
        write_rgba(self.in_dir / "frame_2.png")
        result = pack(self.in_dir, self.out_dir)
        self.assertEqual(result.exit_code, EXIT_OK)

        with Image.open(self.out_dir / "sprite_sheet.png") as sheet:
            self.assertEqual(sheet.mode, "RGBA")
            w = result.frame_width

            rgb_cell = sheet.crop((0, 0, w, result.frame_height))
            # Every alpha value of the RGB-derived cell must be opaque.
            self.assertEqual(rgb_cell.getchannel("A").getextrema(), (255, 255))
            # Original RGB pixel color must survive.
            self.assertEqual(
                rgb_cell.getpixel((w // 2, result.frame_height // 2))[:3],
                (200, 30, 30),
            )

            with Image.open(self.in_dir / "frame_2.png") as original:
                rgba_cell = sheet.crop((w, 0, 2 * w, result.frame_height))
                ox = (w - original.width) // 2
                oy = (result.frame_height - original.height) // 2
                for y in range(original.height):
                    for x in range(original.width):
                        self.assertEqual(
                            rgba_cell.getpixel((x + ox, y + oy)),
                            original.getpixel((x, y)),
                        )

    def test_palette_and_grayscale_frames_normalize(self):
        Image.new("P", (3, 3), 0).save(self.in_dir / "frame_1.png")
        Image.new("L", (3, 3), 128).save(self.in_dir / "frame_2.png")
        Image.new("LA", (3, 3), (64, 200)).save(self.in_dir / "frame_3.png")
        result = pack(self.in_dir, self.out_dir)
        self.assertEqual(
            [f.original_mode for f in result.frames], ["P", "L", "LA"]
        )
        with Image.open(result.sheet_path) as sheet:
            self.assertEqual(sheet.mode, "RGBA")
            # LA frame keeps its alpha 200 instead of being flattened to opaque.
            la_cell = sheet.crop((2 * result.frame_width, 0,
                                  3 * result.frame_width, result.frame_height))
            self.assertEqual(la_cell.getpixel((1, 1))[3], 200)

    # ---- Requirement 3: max-size canvas, centered, transparent padding ----
    def test_canvas_is_max_size_with_centered_transparent_padding(self):
        write_rgb(self.in_dir / "frame_1.png", size=(2, 2))
        write_rgb(self.in_dir / "frame_2.png", size=(4, 4))
        write_rgb(self.in_dir / "frame_3.png", size=(3, 6))
        result = pack(self.in_dir, self.out_dir)
        self.assertEqual((result.frame_width, result.frame_height), (4, 6))

        sheet = Image.open(result.sheet_path)
        self.addCleanup(sheet.close)
        self.assertEqual(sheet.size, (12, 6))

        def expected_cell(size, color):
            frame = Image.new("RGB", size, color).convert("RGBA")
            cell = Image.new("RGBA", (4, 6), (0, 0, 0, 0))
            cell.paste(frame, ((4 - size[0]) // 2, (6 - size[1]) // 2))
            return cell

        specs = [((2, 2), (200, 30, 30)), ((4, 4), (200, 30, 30)), ((3, 6), (200, 30, 30))]
        for slot, (size, color) in enumerate(specs):
            actual = sheet.crop((slot * 4, 0, slot * 4 + 4, 6))
            self.assertEqual(actual.tobytes(), expected_cell(size, color).tobytes())
            # Every pixel outside the centered frame rectangle must be fully
            # transparent (no black borders when composited by a game engine).
            fw, fh = size
            ox, oy = (4 - fw) // 2, (6 - fh) // 2
            for y in range(6):
                for x in range(4):
                    inside = ox <= x < ox + fw and oy <= y < oy + fh
                    self.assertEqual(actual.getpixel((x, y))[3], 255 if inside else 0)

        # 2x2 frame centered at offset (1, 2); its center pixel is opaque.
        cell0 = sheet.crop((0, 0, 4, 6))
        self.assertEqual(cell0.getpixel((1, 2))[:3], (200, 30, 30))
        self.assertEqual(result.frames[0].offset_x, 1)
        self.assertEqual(result.frames[0].offset_y, 2)
        # 4x4 frame centered vertically at offset 0, 1.
        self.assertEqual((result.frames[1].offset_x, result.frames[1].offset_y),
                         (0, 1))

    # ---- Requirement 4: corrupt frames skipped with reason, sheet still made ----
    def test_corrupt_frames_skipped_and_recorded(self):
        write_rgb(self.in_dir / "frame_1.png")
        write_garbage(self.in_dir / "frame_2.png")
        write_truncated(self.in_dir / "frame_3.png")
        result = pack(self.in_dir, self.out_dir)

        self.assertEqual(result.exit_code, EXIT_OK)
        self.assertEqual(result.input_frame_count, 3)
        self.assertEqual(result.valid_frame_count, 1)
        self.assertTrue(result.sheet_path.exists())
        invalid = {item.file: item.reason for item in result.invalid_frames}
        self.assertEqual(set(invalid), {"frame_2.png", "frame_3.png"})
        self.assertTrue(all(reason.strip() for reason in invalid.values()))
        report = read_report(result.report_path)
        self.assertEqual(
            [item["file"] for item in report["invalid_frames"]],
            ["frame_2.png", "frame_3.png"],
        )

    # ---- Requirements 5 & 6: sheet geometry / mode and report completeness ----
    def test_sheet_geometry_and_report_fields(self):
        sizes = [(4, 4), (6, 5), (5, 7)]
        for i, size in enumerate(sizes, start=1):
            write_rgb(self.in_dir / f"frame_{i}.png", size=size)
        result = pack(self.in_dir, self.out_dir)
        sheet = Image.open(result.sheet_path)
        self.addCleanup(sheet.close)

        self.assertEqual(sheet.mode, "RGBA")
        self.assertEqual(sheet.size, (6 * 3, 7))
        self.assertEqual(result.sheet_width, 18)
        self.assertEqual(result.sheet_height, 7)

        report = read_report(result.report_path)
        self.assertEqual(
            set(report),
            {
                "input_frame_count", "valid_frame_count", "invalid_frames",
                "frame_width", "frame_height", "sprite_sheet", "frames",
            },
        )
        self.assertEqual(report["input_frame_count"], 3)
        self.assertEqual(report["valid_frame_count"], 3)
        self.assertEqual(report["invalid_frames"], [])
        self.assertEqual(report["frame_width"], 6)
        self.assertEqual(report["frame_height"], 7)
        self.assertEqual(report["sprite_sheet"],
                         {"file": "sprite_sheet.png", "width": 18,
                          "height": 7, "mode": "RGBA"})
        self.assertEqual([f["x"] for f in report["frames"]], [0, 6, 12])
        for frame in report["frames"]:
            self.assertGreaterEqual(frame["x"], 0)
            self.assertIn("width", frame)
            self.assertIn("height", frame)
            self.assertIn("offset_x", frame)
            self.assertIn("offset_y", frame)
            self.assertIn("original_mode", frame)

    def test_numeric_order_differs_from_lexicographic_order(self):
        # Lexicographic order is frame_10 < frame_8 < frame_9; processing must
        # follow the numeric indices, and the sequence stays contiguous (8..11)
        # so the gap check does not apply.
        colors = {8: (10, 10, 10), 9: (20, 20, 20), 10: (30, 30, 30), 11: (40, 40, 40)}
        for index, color in colors.items():
            write_rgb(self.in_dir / f"frame_{index}.png", color=color)
        result = pack(self.in_dir, self.out_dir)
        self.assertEqual([f.index for f in result.frames], [8, 9, 10, 11])
        self.assertEqual(
            [f.file for f in result.frames],
            ["frame_8.png", "frame_9.png", "frame_10.png", "frame_11.png"],
        )
        with Image.open(result.sheet_path) as sheet:
            w = result.frame_width
            # First cell must hold frame_8's color, not the lexicographic first.
            self.assertEqual(sheet.getpixel((w // 2, sheet.height // 2))[:3],
                             (10, 10, 10))

    # ---- Requirement 7: all-corrupt dir -> nonzero exit, no sheet, report ----
    def test_all_corrupt_nonzero_exit_report_with_zero_valid(self):
        for i in range(1, 4):
            write_garbage(self.in_dir / f"frame_{i}.png")
        result = pack(self.in_dir, self.out_dir)

        self.assertEqual(result.exit_code, EXIT_NO_VALID_FRAMES)
        self.assertFalse(result.sheet_path.exists())
        self.assertTrue(result.report_path.exists())
        report = read_report(result.report_path)
        self.assertEqual(report["valid_frame_count"], 0)
        self.assertEqual(report["input_frame_count"], 3)
        self.assertEqual(len(report["invalid_frames"]), 3)
        self.assertIsNone(report["sprite_sheet"])
        self.assertEqual(report["frames"], [])
        for item in report["invalid_frames"]:
            self.assertTrue(item["file"].endswith(".png"))
            self.assertTrue(item["reason"])

    # ---- Requirement 8: reported X equals actual pixel location ----
    def test_report_coordinates_match_actual_sheet_pixels(self):
        write_rgb(self.in_dir / "frame_1.png", size=(3, 5), color=(7, 77, 177))
        write_rgba(self.in_dir / "frame_2.png", size=(5, 4))
        write_rgb(self.in_dir / "frame_3.png", size=(4, 6), color=(200, 50, 90))
        result = pack(self.in_dir, self.out_dir)
        sheet = Image.open(result.sheet_path)
        self.addCleanup(sheet.close)
        cell_w, cell_h = result.frame_width, result.frame_height

        for record in result.frames:
            # Independently reconstruct the expected cell from the source file.
            source, _ = load_rgba(self.in_dir / record.file)
            expected = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
            expected.paste(
                source,
                ((cell_w - source.width) // 2, (cell_h - source.height) // 2),
            )
            actual = sheet.crop((record.x, 0, record.x + cell_w, cell_h))
            self.assertEqual(actual.tobytes(), expected.tobytes())

        xs = [f.x for f in result.frames]
        self.assertEqual(xs, [i * cell_w for i in range(len(xs))])
        # Last frame's right boundary equals the sheet width exactly.
        self.assertEqual(result.frames[-1].x + cell_w, result.sheet_width)
        self.assertEqual(sheet.width, result.sheet_width)

    # ---- Requirement 9: overwrite / determinism with stale artifacts ----
    def test_repeated_runs_overwrite_and_are_byte_identical(self):
        write_rgb(self.in_dir / "frame_1.png", size=(4, 4))
        write_rgba(self.in_dir / "frame_2.png", size=(5, 5))
        # Stale sheet inside the input dir must be ignored as an input frame
        # and overwritten; an unrelated file must simply remain untouched.
        (self.in_dir / "sprite_sheet.png").write_bytes(b"stale-sheet")
        (self.in_dir / "report.json").write_text("{\"stale\": true}\n")
        (self.in_dir / "notes.txt").write_text("keep me")

        first = pack(self.in_dir)  # default output dir == input dir
        sheet_bytes_1 = first.sheet_path.read_bytes()
        report_1 = read_report(first.report_path)

        second = pack(self.in_dir)
        self.assertEqual(second.sheet_path.read_bytes(), sheet_bytes_1)
        self.assertEqual(read_report(second.report_path), report_1)
        self.assertEqual(second.input_frame_count, 2)  # stale PNG not counted
        self.assertEqual((self.in_dir / "notes.txt").read_text(), "keep me")
        self.assertNotIn("stale", read_report(second.report_path))

        # Separate output directory also overwrites cleanly.
        self.out_dir.mkdir()
        (self.out_dir / "sprite_sheet.png").write_bytes(b"old")
        third = pack(self.in_dir, self.out_dir)
        self.assertEqual(third.sheet_path.read_bytes(), sheet_bytes_1)


class CliEndToEndTests(unittest.TestCase):
    """Exercise generator + CLI through subprocesses in isolated (-I) mode."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

    def test_full_deterministic_workflow(self):
        sample_root = self.tmp / "samples"
        gen = self._run(
            str(PROJECT_ROOT / "generate_samples.py"),
            "--out", str(sample_root),
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)

        # Rebuilding must reproduce byte-identical sample frames.
        sample_root2 = self.tmp / "samples2"
        gen2 = self._run(
            str(PROJECT_ROOT / "generate_samples.py"),
            "--out", str(sample_root2),
        )
        self.assertEqual(gen2.returncode, 0, gen2.stderr)
        for png in sample_root.rglob("*.png"):
            twin = sample_root2 / png.relative_to(sample_root)
            self.assertEqual(png.read_bytes(), twin.read_bytes())

        # Normal scenario: 6 mixed-mode frames, cell 60x48, sheet 360x48.
        out_normal = self.tmp / "out_normal"
        run = self._run(
            str(PROJECT_ROOT / "pack_sprites.py"),
            str(sample_root / "normal"),
            "--output-dir", str(out_normal),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        report = read_report(out_normal / "report.json")
        self.assertEqual(report["valid_frame_count"], 6)
        self.assertEqual(report["input_frame_count"], 6)
        self.assertEqual((report["frame_width"], report["frame_height"]),
                         (60, 48))
        self.assertEqual(report["sprite_sheet"]["width"], 360)
        self.assertEqual(
            [f["original_mode"] for f in report["frames"]],
            ["RGB", "RGBA", "L", "RGBA", "P", "LA"],
        )
        with Image.open(out_normal / "sprite_sheet.png") as sheet:
            self.assertEqual(sheet.mode, "RGBA")
            self.assertEqual(sheet.size, (360, 48))

        # Corrupt scenario: sheet still produced, invalid frame listed.
        out_corrupt = self.tmp / "out_corrupt"
        run = self._run(
            str(PROJECT_ROOT / "pack_sprites.py"),
            str(sample_root / "with_corrupt"),
            "--output-dir", str(out_corrupt),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        report = read_report(out_corrupt / "report.json")
        self.assertEqual(report["valid_frame_count"], 6)
        self.assertEqual(
            [item["file"] for item in report["invalid_frames"]],
            ["frame_07.png"],
        )
        self.assertTrue((out_corrupt / "sprite_sheet.png").exists())

        # All-corrupt: nonzero exit, no sheet, zero-valid report.
        out_all = self.tmp / "out_all"
        run = self._run(
            str(PROJECT_ROOT / "pack_sprites.py"),
            str(sample_root / "all_corrupt"),
            "--output-dir", str(out_all),
        )
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertFalse((out_all / "sprite_sheet.png").exists())
        report = read_report(out_all / "report.json")
        self.assertEqual(report["valid_frame_count"], 0)
        self.assertEqual(len(report["invalid_frames"]), 3)

        # Duplicate and missing index: exit 2 and no products.
        for scenario in ("duplicate_index", "missing_index"):
            out = self.tmp / f"out_{scenario}"
            run = self._run(
                str(PROJECT_ROOT / "pack_sprites.py"),
                str(sample_root / scenario),
                "--output-dir", str(out),
            )
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertFalse((out / "sprite_sheet.png").exists())
            self.assertFalse((out / "report.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
