"""13 项需求的本地确定性验证（标准库 unittest + Pillow）。

运行：python -I -m unittest -v tests.test_tool
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from PIL import Image

from srt_safety.arrangement import FrameSpan, assign_lanes, find_inverted, find_overlaps
from srt_safety.audit import run_audit
from srt_safety.config import ConfigError, load_config
from srt_safety.fonts import load_font
from srt_safety.parser import parse_srt
from srt_safety.timing import frame_number, parse_timecode

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class ToolTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (SAMPLES / "demo.srt").exists():
            subprocess.run(
                [sys.executable, str(ROOT / "generate_samples.py")],
                check=True, cwd=ROOT,
            )
        cls.cfg = load_config(SAMPLES / "demo.config.json")
        cls.font = load_font(cls.cfg.font_size)
        cls.cues, cls.parse_errors = parse_srt(SAMPLES / "demo.srt")
        cls.audit = run_audit(cls.cues, cls.parse_errors, cls.cfg, cls.font)
        cls.records = {r.index: r for r in cls.audit["records"]}

    def fresh_out(self, name: str) -> Path:
        out = ROOT / "output" / name
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        return out


class Requirement01Parsing(ToolTestBase):
    def test_keeps_raw_index_time_text_and_body(self):
        self.assertEqual(len(self.cues), 12)
        first = self.cues[0]
        self.assertEqual(first.index, 1)
        self.assertEqual(first.time_text, "00:00:01,000 --> 00:00:03,000")
        self.assertEqual(first.text, "游戏即将开始，请做好准备。")
        # 显式换行原样保留
        self.assertIn("\n", self.records[8].text)


class Requirement02FrameConversion(ToolTestBase):
    def test_floor_frame_at_30fps(self):
        self.assertEqual(frame_number(1000, 30), 30)          # 题设核对点
        self.assertEqual(frame_number(1500, 30), 45)
        self.assertEqual(frame_number(1234, 30), 37)          # floor(37.02)=37
        self.assertEqual(parse_timecode("00:00:01,000"), 1000)
        rec1 = self.records[1]
        self.assertEqual((rec1.start_frame, rec1.end_frame), (30, 90))


class Requirement03Inversion(ToolTestBase):
    def test_inverted_index_flagged_with_reason(self):
        self.assertIn(10, self.audit["inverted_indexes"])
        self.assertTrue(self.records[10].inverted)
        self.assertIn("倒置", self.records[10].conflict_flags)

    def test_find_inverted_unit(self):
        spans = [FrameSpan(1, 100, 90), FrameSpan(2, 0, 10)]
        self.assertEqual(find_inverted(spans), [1])


class Requirement04Overlap(ToolTestBase):
    def test_overlapping_pairs_listed(self):
        pairs = set(map(tuple, self.audit["overlap_pairs"]))
        # 1[30,90) ∩ 2[60,135)；5/6/7 两两相交
        self.assertIn((1, 2), pairs)
        self.assertIn((5, 6), pairs)
        self.assertIn((5, 7), pairs)
        self.assertIn((6, 7), pairs)
        # 首尾相接不算交叠（9 与 11 等）
        self.assertNotIn((9, 11), pairs)

    def test_find_overlaps_unit(self):
        spans = [FrameSpan(1, 0, 10), FrameSpan(2, 10, 20), FrameSpan(3, 5, 8)]
        self.assertEqual(find_overlaps(spans), [(1, 3)])


class Requirement05Wrapping(ToolTestBase):
    def test_long_text_wraps_within_safe_width(self):
        rec = self.records[3]
        self.assertGreater(rec.line_count, 1)
        for line in rec.lines:
            self.assertLessEqual(line["width_px"], self.cfg.safe_w)

    def test_short_text_single_line(self):
        self.assertEqual(self.records[1].line_count, 1)


class Requirement06Margins(ToolTestBase):
    def test_overwide_cue_marked_with_four_margins(self):
        rec = self.records[4]  # 超长英文 token，单行必然超宽
        self.assertTrue(rec.exceeds_safe_width)
        self.assertTrue(rec.outside_safe)
        for side in ("left", "right", "top", "bottom"):
            self.assertIn(side, rec.margins)
            self.assertIsInstance(rec.margins[side], int)
        self.assertTrue(
            rec.margins["left"] < self.cfg.safe_x
            or rec.margins["right"] < self.cfg.safe_x
        )

    def test_normal_cue_margins_within_safe_area(self):
        rec = self.records[1]
        self.assertFalse(rec.outside_safe)
        self.assertGreaterEqual(rec.margins["left"], self.cfg.safe_x)
        self.assertGreaterEqual(rec.margins["right"], self.cfg.safe_x)
        self.assertGreaterEqual(rec.margins["top"], self.cfg.safe_y)
        self.assertGreaterEqual(rec.margins["bottom"], self.cfg.safe_y)


class Requirement07LaneArrangement(ToolTestBase):
    def test_lanes_no_vertical_overlap(self):
        # 同泳道帧区间不相交
        spans = [
            FrameSpan(r.index, r.start_frame, r.end_frame)
            for r in self.audit["records"] if not r.inverted
        ]
        lanes = assign_lanes(spans)
        occupancy: dict[int, list[tuple[int, int]]] = {}
        for span in spans:
            occupancy.setdefault(lanes[span.index], []).append(
                (span.start_frame, span.end_frame)
            )
        for lane, intervals in occupancy.items():
            intervals.sort()
            for (_, e1), (s2, _) in zip(intervals, intervals[1:]):
                self.assertLessEqual(e1, s2)

        # 5/6/7 三句同屏：代表帧矩形纵向不交叠
        rects = [self.records[i].rect for i in (5, 6, 7)]
        for a, b in ((0, 1), (0, 2), (1, 2)):
            self.assertTrue(
                rects[a]["y1"] <= rects[b]["y0"]
                or rects[b]["y1"] <= rects[a]["y0"],
                "重叠字幕纵向发生交叠",
            )
        # 1/2 同样纵向分离
        r1, r2 = self.records[1].rect, self.records[2].rect
        self.assertTrue(r1["y1"] <= r2["y0"] or r2["y1"] <= r1["y0"])


class Requirement08Rendering(ToolTestBase):
    def test_png_per_valid_cue_with_safe_box(self):
        out = self.fresh_out("req08")
        from srt_safety.cli import main
        rc = main(["--srt", str(SAMPLES / "demo.srt"),
                   "--config", str(SAMPLES / "demo.config.json"),
                   "--out", str(out)])
        self.assertEqual(rc, 0)
        pngs = sorted(out.glob("*.png"))
        self.assertEqual(len(pngs), 12)

        # 每张 PNG 可被 Pillow 打开，尺寸等于画幅，且含安全区明黄像素
        for png in pngs:
            with Image.open(png) as im:
                self.assertEqual(im.size, (self.cfg.width, self.cfg.height))
                colors = im.convert("RGB").getcolors(maxcolors=1 << 20)
            self.assertIsNotNone(colors)
            rgb_values = {c for _, c in colors}
            self.assertIn((255, 214, 64), rgb_values)


class Requirement09Report(ToolTestBase):
    def test_report_fields_and_consistent_stats(self):
        out = self.fresh_out("req09")
        from srt_safety.cli import main
        main(["--srt", str(SAMPLES / "demo.srt"),
              "--config", str(SAMPLES / "demo.config.json"), "--out", str(out)])
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))

        for rec in report["records"]:
            for field in ("index", "time_text", "text", "frame_range",
                          "line_count", "lines", "margins_px",
                          "conflict_flags", "outside_safe"):
                self.assertIn(field, rec)
            self.assertEqual(
                rec["frame_range"]["end"] - rec["frame_range"]["start"] >= 0,
                not rec["inverted"],
            )

        summary = report["summary"]
        self.assertEqual(summary["valid_cues"], len(report["records"]))
        self.assertEqual(
            summary["total_blocks"],
            summary["valid_cues"] + summary["skipped_blocks"],
        )
        self.assertEqual(
            summary["overlap_pairs"], len(report["conflicts"]["overlap_pairs"])
        )
        self.assertEqual(
            summary["inverted_cues"], len(report["conflicts"]["inverted_indexes"])
        )
        self.assertEqual(
            summary["out_of_safe_cues"],
            sum(1 for r in report["records"] if r["outside_safe"]),
        )
        self.assertTrue((out / "report.md").read_text(encoding="utf-8").strip())


class Requirement10BadBlocks(ToolTestBase):
    def test_bad_blocks_skipped_others_processed(self):
        cues, errors = parse_srt(SAMPLES / "bad.srt")
        indexes = {c.index for c in cues}
        self.assertEqual(indexes, {1, 5, 6})           # 好块照常
        self.assertEqual(len(errors), 3)               # 空正文 / 序号非法 / 时间非法
        reasons = " | ".join(e.reason for e in errors)
        self.assertIn("正文", reasons)
        self.assertIn("序号", reasons)
        self.assertIn("时间", reasons)

        out = self.fresh_out("req10")
        from srt_safety.cli import main
        rc = main(["--srt", str(SAMPLES / "bad.srt"),
                   "--config", str(SAMPLES / "demo.config.json"),
                   "--out", str(out)])
        self.assertEqual(rc, 0)
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["records"]), 3)
        self.assertEqual(len(report["parse_errors"]), 3)
        self.assertEqual(sorted(p.name for p in out.glob("*.png")),
                         ["01.png", "05.png", "06.png"])


class Requirement11InvalidConfig(ToolTestBase):
    def test_zero_width_config_aborts_nonzero(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(SAMPLES / "bad.config.json")
        self.assertIn("width", str(ctx.exception))

        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "cli_entry.py"),
             "--srt", str(SAMPLES / "demo.srt"),
             "--config", str(SAMPLES / "bad.config.json"),
             "--out", str(self.fresh_out("req11"))],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("width", proc.stderr)


class Requirement12CliPaths(ToolTestBase):
    def test_explicit_paths_used(self):
        # 输出目录故意不预创建，验证工具自行创建
        out = ROOT / "output" / "req12-custom"
        if out.exists():
            shutil.rmtree(out)
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "cli_entry.py"),
             "--srt", str(SAMPLES / "demo.srt"),
             "--config", str(SAMPLES / "demo.config.json"),
             "--out", str(out)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((out / "report.json").is_file())
        self.assertEqual(len(list(out.glob("*.png"))), 12)


class Requirement13ZeroPadding(ToolTestBase):
    def test_names_padded_to_max_index_digits(self):
        out = self.fresh_out("req13")
        from srt_safety.cli import main
        main(["--srt", str(SAMPLES / "demo.srt"),
              "--config", str(SAMPLES / "demo.config.json"), "--out", str(out)])
        names = sorted(p.name for p in out.glob("*.png"))
        expected = [f"{i:03d}.png" for i in range(1, 13)]  # 最大序号 12 → 3 位补零
        self.assertEqual(names, expected)

        # 单位验证：最大序号为个位数时补 2 位（最大序号位数 + 1）
        out2 = self.fresh_out("req13-bad")
        main(["--srt", str(SAMPLES / "bad.srt"),
              "--config", str(SAMPLES / "demo.config.json"), "--out", str(out2)])
        self.assertEqual(sorted(p.name for p in out2.glob("*.png")),
                         ["01.png", "05.png", "06.png"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
