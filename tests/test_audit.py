"""subtitle_audit 的 unittest 用例（仅标准库，python -m unittest 即可运行）。"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# 子进程需要在任意 cwd 下都能找到本包（默认 out 目录测试会切换 cwd）
CHILD_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

from subtitle_audit.errors import ManifestError
from subtitle_audit.model import (
    OUT_OF_BOUNDS,
    OVERLAP,
    VALID,
    Subtitle,
    audit,
    check_shot_conflicts,
    load_shots,
    load_subtitles,
    natural_key,
    summarize,
)
from subtitle_audit.report import CSV_HEADER, render_png, write_csv


def _write(tmp: Path, name: str, payload) -> Path:
    p = tmp / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _basic_shots():
    return [
        {"shot_id": "S1", "start_frame": 5, "end_frame": 25, "fps": 25},
        {"shot_id": "S2", "start_frame": 25, "end_frame": 50, "fps": 25},
    ]


class RequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- 需求 1：字段齐全性与类型校验 ------------------------------------
    def test_01_missing_key_names_file_and_key(self) -> None:
        shots = _write(self.tmp_path, "shots.json", {"fps": 25, "shots": _basic_shots()})
        subs = _write(self.tmp_path, "subs.json",
                      {"subtitles": [{"id": "E1", "text": "x", "end_frame": 10}]})
        with self.assertRaises(ManifestError) as ctx:
            load_subtitles(subs)
        self.assertIn("subs.json", str(ctx.exception))
        self.assertIn("start_frame", str(ctx.exception))

    def test_01_bad_frame_type_rejected(self) -> None:
        shots = _write(self.tmp_path, "shots.json", {"fps": 25, "shots": _basic_shots()})
        subs = _write(self.tmp_path, "subs.json",
                      {"subtitles": [{"id": "E2", "text": "x",
                                      "start_frame": "10", "end_frame": 12}]})
        with self.assertRaises(ManifestError) as ctx:
            load_subtitles(subs)
        self.assertIn("start_frame", str(ctx.exception))

    def test_01_bool_frame_rejected(self) -> None:
        shots = _write(self.tmp_path, "shots.json",
                       {"fps": 25, "shots": [
                           {"shot_id": "S1", "start_frame": True,
                            "end_frame": 25}]})
        with self.assertRaises(ManifestError):
            load_shots(shots)

    def test_01_missing_fps_rejected(self) -> None:
        shots = _write(self.tmp_path, "shots.json",
                       {"shots": [{"shot_id": "S1", "start_frame": 0,
                                   "end_frame": 25}]})
        with self.assertRaises(ManifestError) as ctx:
            load_shots(shots)
        self.assertIn("fps", str(ctx.exception))

    # --- 需求 2：完全落在镜头 [start,end) 内才归属 -----------------------
    def test_02_containment_half_open(self) -> None:
        rows = audit(load_shots(_write(self.tmp_path, "s.json",
                                       {"fps": 25, "shots": _basic_shots()})),
                     [Subtitle("A", "a", 10, 20, 0)])
        self.assertEqual(rows[0].status, VALID)
        self.assertEqual(rows[0].owner.shot_id, "S1")
        # 结束帧恰好等于镜头终点：半开区间，仍属合法（[10,25) 落在 [5,25)）
        rows2 = audit(load_shots(_write(self.tmp_path, "s2.json",
                                        {"fps": 25, "shots": _basic_shots()})),
                      [Subtitle("B", "b", 10, 25, 0)])
        self.assertEqual(rows2[0].status, VALID)

    # --- 需求 3：越界方向与允许范围 --------------------------------------
    def test_03_early_oob_direction_and_allowed_range(self) -> None:
        rows = audit(load_shots(_write(self.tmp_path, "s.json",
                                       {"fps": 25, "shots": _basic_shots()})),
                     [Subtitle("C", "c", 3, 15, 0)])
        self.assertEqual(rows[0].status, OUT_OF_BOUNDS)
        self.assertIn("第 3 帧", rows[0].conflict)
        self.assertIn("[5,25)", rows[0].conflict)

    def test_03_late_oob(self) -> None:
        rows = audit(load_shots(_write(self.tmp_path, "s.json",
                                       {"fps": 25, "shots": _basic_shots()})),
                     [Subtitle("D", "d", 20, 30, 0)])
        self.assertEqual(rows[0].status, OUT_OF_BOUNDS)
        self.assertIn("晚于镜头终点", rows[0].conflict)

    # --- 需求 4：同镜头重叠冲突对与重叠帧数 -------------------------------
    def test_04_overlap_pair_and_frame_count(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [Subtitle("A", "字幕甲", 10, 20, 0),
                             Subtitle("B", "字幕乙", 15, 25, 0)])
        for row in rows:
            self.assertEqual(row.status, OVERLAP)
            self.assertIn("重叠 5 帧", row.conflict)
        self.assertIn("字幕乙", rows[0].conflict)
        self.assertIn("字幕甲", rows[1].conflict)

    def test_04_touching_intervals_are_allowed(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [Subtitle("A", "甲", 10, 15, 0),
                             Subtitle("B", "乙", 15, 20, 0)])
        self.assertEqual([r.status for r in rows], [VALID, VALID])

    # --- 需求 6/7：无效输入、CSV 状态为空 --------------------------------
    def test_06_07_invalid_equal_and_reversed(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [Subtitle("E", "等长", 30, 30, 0),
                             Subtitle("F", "反向", 40, 20, 0)])
        for row in rows:
            self.assertFalse(row.valid_input)
            self.assertEqual(row.status, "")
            self.assertIn("起止帧无效", row.invalid_reason)
        csv_path = self.tmp_path / "r.csv"
        write_csv(rows, csv_path)
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            table = list(csv.reader(fh))
        self.assertEqual(table[0], CSV_HEADER)
        self.assertEqual(table[1][7], "")          # 状态列空
        self.assertIn("起止帧无效", table[1][8])   # 冲突描述提示原因
        self.assertEqual(table[1][4], "")          # 无效不做毫秒换算

    # --- 需求 8：镜头乱序/重叠警告并继续 ----------------------------------
    def test_08_shot_overlap_warning_and_skip(self) -> None:
        payload = {"fps": 25, "shots": [
            {"shot_id": "A", "start_frame": 10, "end_frame": 20},
            {"shot_id": "B", "start_frame": 5, "end_frame": 15},
        ]}
        shots = load_shots(_write(self.tmp_path, "s.json", payload))
        warnings = check_shot_conflicts(shots)
        self.assertTrue(any("重叠" in w for w in warnings))
        # 起始帧更小的 B[5,15) 保留，A 被剔除；其余不冲突字幕照常处理
        kept = [s.shot_id for s in shots if not s.skipped]
        self.assertEqual(kept, ["B"])
        rows = audit(shots, [Subtitle("G", "不冲突镜头字幕", 7, 12, 0)])
        self.assertEqual(rows[0].status, VALID)
        self.assertEqual(rows[0].owner.shot_id, "B")

    def test_08_disorder_only_does_not_skip_shots(self) -> None:
        payload = {"fps": 25, "shots": [
            {"shot_id": "A", "start_frame": 10, "end_frame": 20},
            {"shot_id": "B", "start_frame": 0, "end_frame": 10},
        ]}
        shots = load_shots(_write(self.tmp_path, "s2.json", payload))
        warnings = check_shot_conflicts(shots)
        self.assertTrue(any("乱序" in w for w in warnings))
        self.assertTrue(all(not s.skipped for s in shots))
        rows = audit(shots, [Subtitle("G", "g", 12, 18, 0)])
        self.assertEqual(rows[0].status, VALID)

    # --- 需求 9：按镜头编号、起始帧排序，忽略输入顺序 ---------------------
    def test_09_sort_independent_of_input_order(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [Subtitle("B", "b", 15, 20, 1),
                             Subtitle("A", "a", 10, 15, 0)])
        self.assertEqual([r.subtitle.sid for r in rows], ["A", "B"])

    def test_09_natural_shot_order(self) -> None:
        self.assertLess(natural_key("S2"), natural_key("S10"))

    # --- 需求 5：PNG 生成 -------------------------------------------------
    def test_05_png_created_and_grows_with_rows(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows1 = audit(shots, [Subtitle(f"T{i}", "t", 6 + i, 8 + i, i) for i in range(3)])
        rows2 = audit(shots, [Subtitle(f"T{i}", "t", 6 + i, 8 + i, i) for i in range(8)])
        p1, p2 = self.tmp_path / "a.png", self.tmp_path / "b.png"
        render_png(rows1, shots, p1)
        render_png(rows2, shots, p2)
        self.assertTrue(p1.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(p2.stat().st_size, 0)

    # --- 需求 11：摘要计数 ------------------------------------------------
    def test_11_summary_counts(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [
            Subtitle("V1", "v", 6, 8, 0),
            Subtitle("V2", "v", 9, 11, 0),
            Subtitle("O1", "o", 0, 6, 0),
            Subtitle("I1", "i", 5, 5, 0),
        ])
        self.assertEqual(summarize(rows),
                         {"有效": 2, "越界": 1, "重叠": 0, "无效": 1})

    def test_11_empty_subtitles_zero_counts(self) -> None:
        shots = load_shots(_write(self.tmp_path, "s.json",
                                  {"fps": 25, "shots": _basic_shots()}))
        rows = audit(shots, [])
        self.assertEqual(summarize(rows),
                         {"有效": 0, "越界": 0, "重叠": 0, "无效": 0})

    # --- 毫秒换算 ---------------------------------------------------------
    def test_ms_conversion_25fps(self) -> None:
        from subtitle_audit.model import frame_to_ms
        self.assertEqual(frame_to_ms(10, 25), 400.0)
        self.assertEqual(frame_to_ms(1, 24), round(1000 / 24, 3))

    # --- 需求 10/11：CLI 端到端、退出码、默认 out 目录 --------------------
    def test_cli_end_to_end_and_exit_codes(self) -> None:
        gen = subprocess.run(
            [sys.executable, str(REPO_ROOT / "samples" / "generate_samples.py")],
            capture_output=True, text=True, cwd=REPO_ROOT, check=True,
        )
        self.assertEqual(gen.returncode, 0)
        data = REPO_ROOT / "samples" / "data"

        # 错误清单：非零退出码，错误信息含文件与键名
        proc_bad = subprocess.run(
            [sys.executable, "-m", "subtitle_audit.cli",
             str(data / "shots.json"), str(data / "subtitles_missing_key.json")],
            capture_output=True, text=True, cwd=REPO_ROOT, env=CHILD_ENV,
        )
        self.assertNotEqual(proc_bad.returncode, 0)
        self.assertIn("start_frame", proc_bad.stderr)
        self.assertIn("subtitles_missing_key.json", proc_bad.stderr)

        # 正常运行：退出码 0，摘要含“有效 N/越界 N”，自动创建 out 目录
        out_dir = self.tmp_path / "custom_out"
        proc_ok = subprocess.run(
            [sys.executable, "-m", "subtitle_audit.cli",
             str(data / "shots.json"), str(data / "subtitles.json"),
             "--csv", str(out_dir / "report.csv"),
             "--png", str(out_dir / "timeline.png")],
            capture_output=True, text=True, cwd=REPO_ROOT, env=CHILD_ENV,
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        self.assertIn("有效 4", proc_ok.stdout)
        self.assertTrue((out_dir / "report.csv").exists())
        self.assertTrue((out_dir / "timeline.png").exists())

        # 默认路径：out/ 自动创建
        proc_default = subprocess.run(
            [sys.executable, "-m", "subtitle_audit.cli",
             str(data / "shots.json"), str(data / "subtitles.json")],
            capture_output=True, text=True, cwd=self.tmp_path, check=True,
            env=CHILD_ENV,
        )
        self.assertTrue((self.tmp_path / "out" / "report.csv").exists())
        self.assertTrue((self.tmp_path / "out" / "timeline.png").exists())

        # 镜头冲突样例：stderr 有警告但退出码为 0
        proc_warn = subprocess.run(
            [sys.executable, "-m", "subtitle_audit.cli",
             str(data / "shots_conflict.json"),
             str(data / "subtitles_conflict.json"),
             "--csv", str(self.tmp_path / "w.csv"),
             "--png", str(self.tmp_path / "w.png")],
            capture_output=True, text=True, cwd=REPO_ROOT, env=CHILD_ENV,
        )
        self.assertEqual(proc_warn.returncode, 0)
        self.assertIn("警告", proc_warn.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
