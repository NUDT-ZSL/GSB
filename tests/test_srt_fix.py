# -*- coding: utf-8 -*-
"""srt_fix.py 的验收测试，覆盖需求 (1)~(15)。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import srt_fix  # noqa: E402


# 便于书写的合法文件：序号 1..n、时间均为 HH:MM:SS,mmm、无重叠。
LEGAL_SRT = """1
00:00:01,000 --> 00:00:02,000
第一句

2
00:00:02,500 --> 00:00:03,500
第二句

3
00:00:04,000 --> 00:00:05,000
第三句
"""


class SrtFixTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.in_dir = self.root / "input"
        self.out_dir = self.root / "output"
        self.in_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 工具方法 ----------
    def write(self, name: str, content: str, encoding: str = "utf-8") -> Path:
        path = self.in_dir / name
        path.write_text(content, encoding=encoding, newline="\n")
        return path

    def write_bytes(self, name: str, content: bytes) -> Path:
        path = self.in_dir / name
        path.write_bytes(content)
        return path

    def run_tool(self, input_dir: Path | None = None, output_dir: Path | None = None):
        """运行 main，捕获标准输出/错误与返回码。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = srt_fix.main(
                [str(input_dir or self.in_dir), str(output_dir or self.out_dir)]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self) -> str:
        report_path = self.out_dir / srt_fix.REPORT_FILENAME
        self.assertTrue(report_path.is_file(), "修正记录未生成")
        return report_path.read_text(encoding="utf-8")

    def out_text(self, name: str) -> str:
        return (self.out_dir / name).read_text(encoding="utf-8")

    def out_entries(self, name: str) -> list[srt_fix.Entry]:
        return srt_fix.parse_srt(self.out_text(name))

    # ---------- (1) 批量处理、同名输出、原文件不变 ----------
    def test_01_batch_process_keeps_originals(self):
        self.write("a.srt", LEGAL_SRT)
        self.write("b.srt", LEGAL_SRT)
        self.write("c.srt", LEGAL_SRT)
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.in_dir.iterdir()
        }

        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        produced = sorted(
            p.name for p in self.out_dir.iterdir() if p.suffix.lower() == ".srt"
        )
        self.assertEqual(produced, ["a.srt", "b.srt", "c.srt"])
        after = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.in_dir.iterdir()
        }
        self.assertEqual(before, after, "原文件被修改")

    # ---------- (2) 时间格式与毫秒补零 ----------
    def test_02_millis_padded_to_three_digits(self):
        self.write(
            "short_ms.srt",
            "1\n00:00:01,50 --> 00:00:02,70\n短毫秒\n",
        )
        self.write(
            "one_ms.srt",
            "1\n00:00:01,5 --> 00:00:02,7\n一位毫秒\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        text = self.out_text("short_ms.srt")
        self.assertIn("00:00:01,050 --> 00:00:02,070", text)
        self.assertNotIn("00:00:01,50 -->", text)
        self.assertIn(
            "00:00:01,005 --> 00:00:02,007", self.out_text("one_ms.srt")
        )

    # ---------- (3) 消除重叠且不丢文本 ----------
    def test_03_overlap_removed_without_losing_text(self):
        self.write(
            "overlap.srt",
            "1\n00:00:01,000 --> 00:00:03,000\n前一句\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n后一句\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("overlap.srt")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].end_ms, entries[1].start_ms)
        self.assertEqual(entries[0].text, "前一句")
        self.assertEqual(entries[1].text, "后一句")
        self.assertFalse(srt_fix.has_overlap(entries))

    # ---------- (4) 颠倒条目标注且不影响其余条目 ----------
    def test_04_reversed_entry_removed_and_reported(self):
        self.write(
            "reversed.srt",
            "1\n00:00:01,000 --> 00:00:02,000\n正常一\n\n"
            "2\n00:00:10,000 --> 00:00:08,000\n颠倒\n\n"
            "3\n00:00:11,000 --> 00:00:12,000\n正常二\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("reversed.srt")
        self.assertEqual([e.text for e in entries], ["正常一", "正常二"])
        self.assertEqual(entries[0].start_ms, 1000)
        self.assertEqual(entries[0].end_ms, 2000)
        self.assertEqual(entries[1].start_ms, 11000)
        self.assertEqual(entries[1].end_ms, 12000)

        report = self.report()
        self.assertIn("reversed.srt", report)
        self.assertIn("条目 #2", report)
        self.assertIn("00:00:10,000 --> 00:00:08,000", report)
        self.assertIn("时间颠倒", report)

    # ---------- (5) 空文本/空白文本移除并记录序号 ----------
    def test_05_empty_text_entry_removed_and_reported(self):
        self.write(
            "empty_text.srt",
            "1\n00:00:01,000 --> 00:00:02,000\n有字\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n   \n\n"
            "3\n00:00:03,000 --> 00:00:04,000\n有字二\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("empty_text.srt")
        self.assertEqual([e.text for e in entries], ["有字", "有字二"])

        report = self.report()
        self.assertIn("条目 #2", report)
        self.assertIn("已移除", report)
        self.assertIn("空文本", report)

    # ---------- (6) 序号重排 ----------
    def test_06_indices_renumbered(self):
        self.write(
            "bad_index.srt",
            "1\n00:00:01,000 --> 00:00:02,000\n一\n\n"
            "3\n00:00:02,500 --> 00:00:03,500\n二\n\n"
            "3\n00:00:04,000 --> 00:00:05,000\n三\n\n"
            "7\n00:00:05,500 --> 00:00:06,500\n四\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        text = self.out_text("bad_index.srt")
        self.assertTrue(
            text.startswith("1\n00:00:01,000"), "输出未从 1 开始编号"
        )
        entries = self.out_entries("bad_index.srt")
        self.assertEqual(
            [e.declared_index for e in entries], [1, 2, 3, 4]
        )
        self.assertIn("序号规范化", self.report())

    # ---------- (7) 修正记录内容 ----------
    def test_07_report_has_filename_and_old_new_times(self):
        self.write(
            "overlap2.srt",
            "1\n00:00:01,000 --> 00:00:03,000\n甲\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n乙\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        report = self.report()
        self.assertIn("overlap2.srt", report)
        self.assertIn("原时间: 00:00:02,000 --> 00:00:04,000", report)
        self.assertIn("新时间: 00:00:03,000 --> 00:00:04,000", report)
        self.assertIn("时间重叠", report)
        self.assertIn("条目 #2", report)

    # ---------- (8) 合法文件保持不变并注明无需修正 ----------
    def test_08_legal_file_unchanged(self):
        self.write("legal.srt", LEGAL_SRT)
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        self.assertEqual(self.out_text("legal.srt"), LEGAL_SRT)
        self.assertIn("legal.srt", self.report())
        self.assertIn("无需修正", self.report())

    # ---------- (9) 无法解析文件不中断其他文件 ----------
    def test_09_unparsable_file_reported_without_aborting(self):
        self.write_bytes("empty.srt", b"")
        self.write("good1.srt", LEGAL_SRT)
        self.write("good2.srt", LEGAL_SRT)

        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        self.assertTrue((self.out_dir / "good1.srt").is_file())
        self.assertTrue((self.out_dir / "good2.srt").is_file())
        self.assertFalse((self.out_dir / "empty.srt").exists())

        report = self.report()
        self.assertIn("empty.srt", report)
        self.assertIn("未能处理", report)

    # ---------- (10) BOM 兼容 ----------
    def test_10_bom_input_output_without_bom(self):
        self.write_bytes(
            "bom.srt", b"\xef\xbb\xbf" + LEGAL_SRT.encode("utf-8")
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        raw = (self.out_dir / "bom.srt").read_bytes()
        self.assertEqual(raw[:1], b"1")
        self.assertNotIn("﻿", raw.decode("utf-8"))
        self.assertNotIn("﻿", self.out_text("bom.srt"))

    # ---------- (11) 多行文本原样保留 ----------
    def test_11_multiline_text_preserved(self):
        self.write(
            "multi.srt",
            "1\n00:00:01,000 --> 00:00:02,000\n第一行\n第二行\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("multi.srt")
        self.assertEqual(entries[0].text_lines, ["第一行", "第二行"])
        self.assertEqual(entries[0].text, "第一行\n第二行")

    # ---------- (12) 忽略非 srt 文件 ----------
    def test_12_non_srt_ignored(self):
        self.write("legal.srt", LEGAL_SRT)
        self.write("备注.txt", "这不是字幕\n")
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        names = sorted(p.name for p in self.out_dir.iterdir())
        self.assertEqual(names, ["legal.srt", srt_fix.REPORT_FILENAME])
        self.assertNotIn("备注.txt", self.report())

    # ---------- (13) 路径校验 ----------
    def test_13_output_created_and_bad_input_fails(self):
        self.write("legal.srt", LEGAL_SRT)
        code, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertTrue(self.out_dir.is_dir())

        missing_in = self.root / "no_such_dir"
        missing_out = self.root / "no_such_output"
        code, _, stderr = self.run_tool(missing_in, missing_out)

        self.assertNotEqual(code, 0)
        self.assertTrue(stderr.strip(), "标准错误无提示")
        self.assertIn("不存在", stderr)
        self.assertFalse(missing_out.exists(), "非法输入时仍创建了输出目录")

    def test_13b_input_is_file_not_dir(self):
        a_file = self.in_dir / "legal.srt"
        self.write("legal.srt", LEGAL_SRT)
        out = self.root / "out2"

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = srt_fix.main([str(a_file), str(out)])

        self.assertNotEqual(code, 0)
        self.assertIn("不是目录", stderr.getvalue())
        self.assertFalse(out.exists())

    # ---------- (14) 越界时改为调整前一条终点 ----------
    def test_14_fallback_adjusts_previous_end(self):
        self.write(
            "nested.srt",
            "1\n00:00:01,000 --> 00:00:05,000\n长条目\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n嵌套条目\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("nested.srt")
        self.assertEqual(entries[0].end_ms, 3000)
        self.assertEqual(entries[1].start_ms, 3000)
        self.assertEqual(entries[1].end_ms, 4000)
        self.assertFalse(srt_fix.has_overlap(entries))

        report = self.report()
        self.assertIn("调整前一条终点", report)
        self.assertIn("条目 #1", report)

    def test_14b_contained_and_equal_end(self):
        self.write(
            "nested2.srt",
            "1\n00:00:01,000 --> 00:00:05,000\n长条目\n\n"
            "2\n00:00:04,800 --> 00:00:05,000\n末尾对齐\n",
        )
        self.write(
            "nested3.srt",
            "1\n00:00:01,000 --> 00:00:05,000\n长条目\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n完全包含\n",
        )
        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        for name in ("nested2.srt", "nested3.srt"):
            entries = self.out_entries(name)
            self.assertFalse(srt_fix.has_overlap(entries), name)
            self.assertGreaterEqual(entries[1].start_ms, entries[0].end_ms)
            self.assertGreaterEqual(entries[1].end_ms, entries[1].start_ms)
            self.assertEqual(len(entries), 2)
        self.assertIn("调整前一条终点", self.report())

    # ---------- (15) 汇总 ----------
    def test_15_summary_counts(self):
        self.write(
            "fix_a.srt",
            "1\n00:00:01,000 --> 00:00:03,000\n甲\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n乙\n",
        )
        self.write("fix_b.srt", "1\n00:00:01,50 --> 00:00:02,00\n毫秒\n")
        self.write("clean.srt", LEGAL_SRT)

        code, stdout, _ = self.run_tool()

        self.assertEqual(code, 0)
        report = self.report()
        self.assertIn(
            "汇总: 成功文件数: 3, 含修正文件数: 2, 未能处理文件数: 0",
            report,
        )
        self.assertIn("成功 3 个, 含修正 2 个", stdout)

    # ---------- 附加：混合异常场景不崩溃 ----------
    def test_16_mixed_cases_end_to_end(self):
        self.write(
            "mixed.srt",
            "3\n00:00:01,000 --> 00:00:03,000\n一\n第二行\n\n"
            "3\n00:00:02,000 --> 00:00:04,000\n二\n\n"
            "9\n00:00:09,000 --> 00:00:07,000\n颠倒\n\n"
            "2\n00:00:00,800 --> 00:00:01,000\n   \n",
        )
        self.write_bytes("broken.srt", b"\x00\x01binary")
        self.write("ok.srt", LEGAL_SRT)

        code, _, _ = self.run_tool()

        self.assertEqual(code, 0)
        entries = self.out_entries("mixed.srt")
        self.assertEqual([e.text for e in entries], ["一\n第二行", "二"])
        self.assertFalse(
            srt_fix.has_overlap(entries), "输出仍存在重叠"
        )
        self.assertTrue((self.out_dir / "ok.srt").is_file())
        self.assertFalse((self.out_dir / "broken.srt").exists())
        report = self.report()
        self.assertIn("broken.srt", report)
        self.assertIn("未能处理", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
