"""wav_splitter 全量验收测试（python -m unittest -v）。

覆盖任务书第 1~14 条全部验收点，使用临时目录，不依赖任何第三方库。
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr

from wav_splitter.cli import run
from wav_splitter.errors import SplitterError
from wav_splitter.markers import load_markers
from wav_splitter.models import (
    STATUS_INVALID_INTERVAL,
    STATUS_OUT_OF_RANGE,
    STATUS_OVERLAP,
    STATUS_SUCCESS,
)
from wav_splitter.processor import process_markers
from wav_splitter.report import REPORT_FILENAME, build_report
from wav_splitter.wav_io import load_wav, time_to_sample_index
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_wav(path: str, *, rate: int = 8000, width: int = 2,
             channels: int = 1, frames: int = 32000) -> None:
    """生成确定性测试 WAV（未压缩 PCM）。"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        pcm = bytearray()
        for i in range(frames):
            pcm += struct.pack("<h", (i * 7) % 65536 - 32768)
        wf.writeframes(bytes(pcm))


def write_csv(path: str, rows: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(rows) + "\n")


def write_ulaw_wav(path: str, *, rate: int = 8000, frames: int = 100) -> None:
    """手工拼一个格式标记为 μ-law(7) 的压缩 WAV（CPython wave 不允许直接写）。"""
    payload = b"\x7f" * frames
    # WAVEFORMATEX: formatTag=μ-law, channels=1, sampleRate, byteRate,
    # blockAlign, bitsPerSample=8
    fmt_chunk = struct.pack("<HHIIHH", 7, 1, rate, rate, 1, 8)
    riff_size = 4 + (8 + 16) + (8 + len(payload))
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<I", 16) + fmt_chunk)
        fh.write(b"data" + struct.pack("<I", len(payload)) + payload)


def status_map(report: dict) -> dict:
    return {e["编号"]: e for e in report["片段结果"]}


class WavSplitterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.wav_path = os.path.join(self.tmp, "in.wav")
        self.csv_path = os.path.join(self.tmp, "marks.csv")
        self.out_dir = os.path.join(self.tmp, "out")
        make_wav(self.wav_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def execute(self, rows: list[str]):
        write_csv(self.csv_path, rows)
        os.makedirs(self.out_dir, exist_ok=True)
        markers = load_markers(self.csv_path)
        info, pcm = load_wav(self.wav_path)
        results = process_markers(markers, info, pcm, self.out_dir)
        return results, info, pcm

    def run_cli(self, wav: str | None = None, csv: str | None = None,
                out: str | None = None):
        return run([
            "--wav", wav or self.wav_path,
            "--csv", csv or self.csv_path,
            "--output", out or self.out_dir,
        ])


class TestRequirement01Traversal(WavSplitterTestBase):
    """1. 程序遍历全部标记行并进入处理。"""

    def test_all_rows_processed(self):
        rows = ["A,0,1", "B,1,2", "C,2,3"]
        write_csv(self.csv_path, rows)
        code = self.run_cli()
        self.assertEqual(code, 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["统计"]["标记总数"], 3)
        self.assertEqual([e["编号"] for e in report["片段结果"]],
                         ["A", "B", "C"])


class TestRequirement02OutOfRange(WavSplitterTestBase):
    """2. 越界标记登记并跳过，无 WAV 生成，保留编号与原始时间。"""

    def test_out_of_range(self):
        rows = ["X,-1,5"]
        write_csv(self.csv_path, rows)
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = self.run_cli()
        self.assertEqual(code, 0)
        self.assertNotIn("X.wav", os.listdir(self.out_dir))
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entry = json.load(fh)["片段结果"][0]
        self.assertEqual(entry["状态"], STATUS_OUT_OF_RANGE)
        self.assertEqual(entry["原始起始时间"], "-1")
        self.assertEqual(entry["原始结束时间"], "5")
        self.assertIn("越界", buf.getvalue())
        self.assertIn("X", buf.getvalue())


class TestRequirement03InvalidInterval(WavSplitterTestBase):
    """3. 结束 <= 起始登记为无效区间并跳过。"""

    def test_equal_endpoints(self):
        rows = ["F,2,2"]
        write_csv(self.csv_path, rows)
        code = self.run_cli()
        self.assertEqual(code, 0)
        self.assertNotIn("F.wav", os.listdir(self.out_dir))
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entry = json.load(fh)["片段结果"][0]
        self.assertEqual(entry["状态"], STATUS_INVALID_INTERVAL)
        self.assertTrue(entry["错误描述"])

    def test_end_before_start(self):
        results, info, pcm = self.execute(["F,3,2"])
        self.assertEqual(results[0].status, STATUS_INVALID_INTERVAL)


class TestRequirement04Overlap(WavSplitterTestBase):
    """4. 端点重叠时后出现者登记重叠并跳过，先出现者正常导出。"""

    def test_overlap_skips_later(self):
        rows = ["A,1,3", "B,2,4"]
        write_csv(self.csv_path, rows)
        code = self.run_cli()
        self.assertEqual(code, 0)
        files = os.listdir(self.out_dir)
        self.assertIn("A.wav", files)
        self.assertNotIn("B.wav", files)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entries = status_map(json.load(fh))
        self.assertEqual(entries["A"]["状态"], STATUS_SUCCESS)
        self.assertEqual(entries["B"]["状态"], STATUS_OVERLAP)

    def test_touching_endpoint_is_overlap(self):
        # 端点相接 [0,1] 与 [1,2]：含端点重叠，后者跳过
        results, _, _ = self.execute(["A,0,1", "B,1,2"])
        self.assertEqual([r.status for r in results],
                         [STATUS_SUCCESS, STATUS_OVERLAP])


class TestRequirement05HeaderPreservation(WavSplitterTestBase):
    """5. 导出 WAV 的采样率/位深/声道与原文件一致，时长吻合。"""

    def test_header_and_duration(self):
        rows = ["A,1,3"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        with wave.open(self.wav_path, "rb") as src, \
                wave.open(os.path.join(self.out_dir, "A.wav"), "rb") as out:
            self.assertEqual(out.getframerate(), src.getframerate())
            self.assertEqual(out.getsampwidth(), src.getsampwidth())
            self.assertEqual(out.getnchannels(), src.getnchannels())
            # 2 秒 @8000Hz
            self.assertEqual(out.getnframes(), 16000)
            self.assertAlmostEqual(
                out.getnframes() / out.getframerate(), 2.0, places=6)

    def test_other_format_preserved(self):
        # 换一套参数（16000Hz / 8bit）再验一次
        alt_wav = os.path.join(self.tmp, "alt.wav")
        make_wav(alt_wav, rate=16000, width=1, frames=16000)
        write_csv(self.csv_path, ["A,0.5,1"])
        code = self.run_cli(wav=alt_wav)
        self.assertEqual(code, 0)
        with wave.open(alt_wav, "rb") as src, \
                wave.open(os.path.join(self.out_dir, "A.wav"), "rb") as out:
            self.assertEqual(out.getframerate(), 16000)
            self.assertEqual(out.getsampwidth(), 1)
            self.assertEqual(out.getnchannels(), 1)
            self.assertEqual(out.getnframes(), 8000)


class TestRequirement06ReportFields(WavSplitterTestBase):
    """6. JSON 报告字段齐全、类型正确，成功项错误描述为 null。"""

    def test_report_schema(self):
        rows = ["A,0,1", "E,-1,5", "F,2,2", "G,0.5,0.8", "b,3,3.5"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            report = json.load(fh)
        for key in ("报告版本", "源文件", "音频参数", "统计", "片段结果"):
            self.assertIn(key, report)
        entries = status_map(report)
        for entry in report["片段结果"]:
            self.assertIsInstance(entry["编号"], str)
            self.assertIsInstance(entry["原始起始时间"], str)
            self.assertIsInstance(entry["原始结束时间"], str)
            self.assertIn(entry["状态"],
                          {STATUS_SUCCESS, STATUS_OUT_OF_RANGE,
                           STATUS_INVALID_INTERVAL, STATUS_OVERLAP})
        success = entries["A"]
        self.assertIsNone(success["错误描述"])
        self.assertEqual(success["导出文件"], "A.wav")
        self.assertIsInstance(success["起始采样点"], int)
        # 跳过项必须是错误描述字符串，且不得带导出文件字段
        for skipped_id in ("E", "F", "G"):
            entry = entries[skipped_id]
            self.assertIsInstance(entry["错误描述"], str)
            self.assertNotIn("导出文件", entry)
            self.assertNotIn("起始采样点", entry)
        # 成功片段 b 同样错误描述为 null，且排序在 A 之后
        self.assertIsNone(entries["b"]["错误描述"])


class TestRequirement07BadCsv(WavSplitterTestBase):
    """7. CSV 缺失/格式错误：非 0 退出、明确原因、无任何输出文件。"""

    def _assert_fatal_with_no_output(self, rows: list[str] | None,
                                     missing: bool = False):
        sentinel = os.path.join(self.tmp, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write("keep")
        if missing:
            csv_arg = os.path.join(self.tmp, "nope.csv")
        else:
            write_csv(self.csv_path, rows)  # type: ignore[arg-type]
            csv_arg = self.csv_path
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = self.run_cli(csv=csv_arg)
        self.assertNotEqual(code, 0)
        self.assertTrue(buf.getvalue().strip())
        # 输出目录不应被创建；临时目录里除哨兵外无新增
        self.assertFalse(os.path.exists(self.out_dir))
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         sorted(["in.wav", "keep.txt"] +
                                ([] if missing else ["marks.csv"])))

    def test_missing_csv(self):
        self._assert_fatal_with_no_output(None, missing=True)

    def test_missing_column(self):
        self._assert_fatal_with_no_output(["A,1"])

    def test_non_numeric_time(self):
        self._assert_fatal_with_no_output(["A,abc,2"])

    def test_too_many_columns(self):
        self._assert_fatal_with_no_output(["A,1,2,extra"])

    def test_empty_id(self):
        self._assert_fatal_with_no_output([",1,2"])


class TestRequirement08BadWav(WavSplitterTestBase):
    """8. WAV 缺失或非有效单声道 PCM：非 0 退出、无输出文件。"""

    def _assert_wav_fatal(self, wav_arg: str):
        write_csv(self.csv_path, ["A,0,1"])
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = self.run_cli(wav=wav_arg)
        self.assertNotEqual(code, 0)
        self.assertTrue(buf.getvalue().strip())
        self.assertFalse(os.path.exists(self.out_dir))

    def test_missing_wav(self):
        self._assert_wav_fatal(os.path.join(self.tmp, "nope.wav"))

    def test_garbage_wav(self):
        bad = os.path.join(self.tmp, "garbage.wav")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a wav file at all")
        self._assert_wav_fatal(bad)

    def test_compressed_wav(self):
        bad = os.path.join(self.tmp, "compressed.wav")
        write_ulaw_wav(bad)
        self._assert_wav_fatal(bad)

    def test_stereo_wav(self):
        bad = os.path.join(self.tmp, "stereo.wav")
        make_wav(bad, channels=2, frames=8000)
        self._assert_wav_fatal(bad)


class TestRequirement09CliAndMkdir(WavSplitterTestBase):
    """9. 命令行三参数，输出目录不存在时自动创建并写入产物。"""

    def test_creates_output_dir(self):
        nested = os.path.join(self.tmp, "a", "b", "c")
        write_csv(self.csv_path, ["A,0,1"])
        code = self.run_cli(out=nested)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(nested))
        self.assertIn("A.wav", os.listdir(nested))
        self.assertIn(REPORT_FILENAME, os.listdir(nested))


class TestRequirement10StderrWarnings(WavSplitterTestBase):
    """10. 跳过片段向 stderr 输出含编号与原因的警告。"""

    def test_warning_content(self):
        # A[0,1] 先成功，G[0.5,0.8] 后出现并与 A 重叠；b 与已接受片段不相交
        write_csv(self.csv_path,
                  ["E,-1,5", "F,2,2", "A,0,1", "G,0.5,0.8", "b,3,3.5"])
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.run_cli()
        text = buf.getvalue()
        self.assertIn("E", text)
        self.assertIn("越界", text)
        self.assertIn("F", text)
        self.assertIn("无效区间", text)
        self.assertIn("G", text)
        self.assertIn("重叠", text)
        # 成功片段不应产生警告
        self.assertNotIn("片段 A", text)
        self.assertNotIn("片段 b", text)


class TestRequirement11Sorting(WavSplitterTestBase):
    """11. 报告按编号大小写不敏感升序，同编号按 CSV 行号升序。"""

    def test_case_insensitive_order(self):
        # “b”与“A”大小写不敏感排序 -> A 在 b 前（任务 11 验收点）；
        # 重复编号 x 再验证同编号按 CSV 原始行号升序。
        # 区间互相留间隔，避免触发端点相接的重叠规则。
        rows = ["b,0,0.2", "A,0.3,0.5", "x,0.6,0.65", "x,0.7,0.75"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entries = json.load(fh)["片段结果"]
        keys = [(e["编号"], e["csv行号"]) for e in entries]
        self.assertEqual(keys, [("A", 2), ("b", 1), ("x", 3), ("x", 4)])
        # 重复编号的两个文件都应存在且文件名唯一
        files = os.listdir(self.out_dir)
        self.assertIn("x.wav", files)
        self.assertIn("x__row4.wav", files)

    def test_casefold_tie_order(self):
        # 编号 B 与 b 的 casefold 相同：并列时回到 CSV 行号
        rows = ["b,0,0.2", "B,0.3,0.5"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entries = json.load(fh)["片段结果"]
        self.assertEqual([(e["编号"], e["csv行号"]) for e in entries],
                         [("b", 1), ("B", 2)])


class TestRequirement12FloatAlignment(WavSplitterTestBase):
    """12. 浮点秒 + 毫秒级对齐：1.2345s @8000Hz -> 首采样点 9880。"""

    def test_alignment_math(self):
        self.assertEqual(
            time_to_sample_index(Decimal("1.2345"), 8000), 9880)
        # 半值向上：1.23455 秒毫秒量化为 1.235 -> 9880
        self.assertEqual(
            time_to_sample_index(Decimal("1.23455"), 8000), 9880)
        # 1.2344 秒毫秒量化为 1.234 -> 9872
        self.assertEqual(
            time_to_sample_index(Decimal("1.2344"), 8000), 9872)

    def test_exported_first_sample_index_and_length(self):
        rows = ["c,1.2345,1.4"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            entry = json.load(fh)["片段结果"][0]
        self.assertEqual(entry["起始采样点"], 9880)
        # 1.4s -> 11200；长度 11200 - 9880 = 1320 帧 = 0.165s
        self.assertEqual(entry["结束采样点"], 11200)
        with wave.open(os.path.join(self.out_dir, "c.wav"), "rb") as wf:
            self.assertEqual(wf.getnframes(), 1320)
            data = wf.readframes(wf.getnframes())
        # 用原始斜坡取值反查首采样点确实是第 9880 个点
        first_value = struct.unpack("<h", data[:2])[0]
        expected = (9880 * 7) % 65536 - 32768
        self.assertEqual(first_value, expected)


class TestRequirement13NoPhantomSegments(WavSplitterTestBase):
    """13. 跳过/缺失片段不得当作空片段写入；合法片段全部导出。"""

    def test_mixed_inputs(self):
        # A[0,1] 成功；B[1,2] 与 A 端点相接 -> 重叠跳过；
        # E 越界、F 无效区间；G 与已接受片段不相交 -> 成功
        rows = ["A,0,1", "B,1,2", "E,-1,5", "F,2,2", "G,2.5,2.8"]
        write_csv(self.csv_path, rows)
        self.assertEqual(self.run_cli(), 0)
        wav_files = sorted(f for f in os.listdir(self.out_dir)
                           if f.endswith(".wav"))
        self.assertEqual(wav_files, ["A.wav", "G.wav"])
        for name in wav_files:
            with wave.open(os.path.join(self.out_dir, name), "rb") as wf:
                self.assertGreater(wf.getnframes(), 0)
        with open(os.path.join(self.out_dir, REPORT_FILENAME),
                  encoding="utf-8") as fh:
            report = json.load(fh)
        entries = status_map(report)
        self.assertEqual(entries["B"]["状态"], STATUS_OVERLAP)
        for skipped_id in ("E", "F", "B"):
            self.assertNotIn("导出文件", entries[skipped_id])
        self.assertEqual(report["统计"]["成功数"], 2)
        self.assertEqual(report["统计"]["重叠数"], 1)
        self.assertEqual(report["统计"]["标记总数"], 5)


class TestRequirement14DeterministicSamples(unittest.TestCase):
    """14. 生成脚本确定性产出，且报告结构与说明一致。"""

    def test_generator_and_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            gen = subprocess.run(
                [sys.executable, "-I", "-X", "utf8",
                 os.path.join(REPO_ROOT, "tools", "generate_samples.py"),
                 "--dir", tmp],
                capture_output=True, encoding="utf-8", errors="replace",
                cwd=REPO_ROOT,
            )
            self.assertEqual(gen.returncode, 0, gen.stderr)
            wav_path = os.path.join(tmp, "sample.wav")
            csv_path = os.path.join(tmp, "marks.csv")
            out_dir = os.path.join(tmp, "out")

            # 再跑一次，WAV 必须逐字节一致（确定性）
            tmp2 = os.path.join(tmp, "again")
            gen2 = subprocess.run(
                [sys.executable, "-I", "-X", "utf8",
                 os.path.join(REPO_ROOT, "tools", "generate_samples.py"),
                 "--dir", tmp2],
                capture_output=True, encoding="utf-8", errors="replace",
                cwd=REPO_ROOT,
            )
            self.assertEqual(gen2.returncode, 0, gen2.stderr)
            with open(os.path.join(tmp, "sample.wav"), "rb") as a, \
                    open(os.path.join(tmp2, "sample.wav"), "rb") as b:
                self.assertEqual(a.read(), b.read())

            code = run(["--wav", wav_path, "--csv", csv_path,
                        "--output", out_dir])
            self.assertEqual(code, 0)
            with open(os.path.join(out_dir, REPORT_FILENAME),
                      encoding="utf-8") as fh:
                report = json.load(fh)

            entries = status_map(report)
            self.assertEqual(entries["A"]["状态"], STATUS_SUCCESS)
            self.assertEqual(entries["b"]["状态"], STATUS_SUCCESS)
            self.assertEqual(entries["c"]["状态"], STATUS_SUCCESS)
            self.assertEqual(entries["c"]["起始采样点"], 9880)
            self.assertEqual(entries["D"]["状态"], STATUS_SUCCESS)
            self.assertEqual(entries["E"]["状态"], STATUS_OUT_OF_RANGE)
            self.assertEqual(entries["F"]["状态"], STATUS_INVALID_INTERVAL)
            self.assertEqual(entries["G"]["状态"], STATUS_OVERLAP)
            # 报告排序按 casefold：A,b,c,D,E,F,G（b/c 小写排在 D 前）
            self.assertEqual(
                [e["编号"] for e in report["片段结果"]],
                ["A", "b", "c", "D", "E", "F", "G"],
            )
            wav_files = sorted(f for f in os.listdir(out_dir)
                               if f.endswith(".wav"))
            self.assertEqual(wav_files, ["A.wav", "D.wav", "b.wav", "c.wav"])


class TestBoundaryAndSubprocess(unittest.TestCase):
    """补充：边界端点、子进程退出码、Python 解释器 -I 隔离运行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.wav_path = os.path.join(self.tmp, "in.wav")
        self.csv_path = os.path.join(self.tmp, "marks.csv")
        self.out_dir = os.path.join(self.tmp, "out")
        make_wav(self.wav_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_duration_endpoint_is_valid(self):
        write_csv(self.csv_path, ["A,0,4"])
        self.assertEqual(
            run(["--wav", self.wav_path, "--csv", self.csv_path,
                 "--output", self.out_dir]), 0)
        with wave.open(os.path.join(self.out_dir, "A.wav"), "rb") as wf:
            self.assertEqual(wf.getnframes(), 32000)

    def test_subprocess_bad_csv_exit_code(self):
        write_csv(self.csv_path, ["A,1"])  # 缺列
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8",
             os.path.join(REPO_ROOT, "tests", "_isolated_runner.py"),
             "--wav", self.wav_path, "--csv", self.csv_path,
             "--output", self.out_dir],
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=REPO_ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("列数", proc.stderr)
        self.assertFalse(os.path.exists(self.out_dir))

    def test_subprocess_missing_wav_exit_code(self):
        write_csv(self.csv_path, ["A,0,1"])
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8",
             os.path.join(REPO_ROOT, "tests", "_isolated_runner.py"),
             "--wav", os.path.join(self.tmp, "nope.wav"),
             "--csv", self.csv_path, "--output", self.out_dir],
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=REPO_ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不存在", proc.stderr)
        self.assertFalse(os.path.exists(self.out_dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
