#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收测试：覆盖任务说明中的 12 条要求。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_acceptance.py
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "audio_merge.py"
RATE = 16_000


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def write_wav(path: Path, samples, rate: int = RATE,
              channels: int = 1, sample_width: int = 2) -> Path:
    data = array("h", samples)
    if channels > 1:
        interleaved = array("h")
        for value in data:
            interleaved.extend([value] * channels)
        data = interleaved
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.setcomptype("NONE", "NONE")
        wf.writeframes(data.tobytes())
    return path


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.getnframes()
        raw = wf.readframes(frames)
    samples = array("h")
    samples.frombytes(raw)
    return rate, width, channels, samples


def constant(value: int, duration: float, rate: int = RATE):
    return [value] * int(round(duration * rate))


def square(duration: float, rate: int = RATE):
    half = max(1, rate // 400)  # 200 Hz
    return [32767 if (i // half) % 2 == 0 else -32767
            for i in range(int(round(duration * rate)))]


def run_cli(track0, track1, intervals, out_dir, use_flags: bool = False):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if use_flags:
        command = [sys.executable, str(SCRIPT),
                   "--track0", str(track0), "--track1", str(track1),
                   "--intervals", str(intervals), "--out-dir", str(out_dir)]
    else:
        command = [sys.executable, str(SCRIPT), str(track0), str(track1),
                   str(intervals), str(out_dir)]
    return subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def dump_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class CLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out_dir = self.tmp / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_tracks(self, track0_value: int = 10_000,
                    track1_value: int = -10_000,
                    duration: float = 3.0,
                    rate: int = RATE):
        t0 = write_wav(self.tmp / "track0.wav",
                       constant(track0_value, duration, rate), rate)
        t1 = write_wav(self.tmp / "track1.wav",
                       constant(track1_value, duration, rate), rate)
        return t0, t1

    def make_intervals(self, entries: list, name: str = "intervals.json") -> Path:
        return dump_json(self.tmp / name, entries)

    def assert_report(self, out_dir: Path) -> dict:
        return json.loads((out_dir / "out_report.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. 命令行生成 out.wav / out_report.json，内容符合语义
# ---------------------------------------------------------------------------

class TestRequirement01(CLITestCase):
    def test_creates_files_with_expected_content(self):
        t0, t1 = self.make_tracks(track0_value=10_000, track1_value=-10_000,
                                  duration=2.0)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.2, "end": 0.7, "fade": 0.0},
            {"track": 1, "start": 0.4, "end": 1.0, "fade": 0.0},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue((self.out_dir / "out.wav").is_file())
        self.assertTrue((self.out_dir / "out_report.json").is_file())

        rate, width, channels, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(rate, RATE)
        self.assertEqual(width, 2)
        self.assertEqual(channels, 1)
        # 0.5s 轨0（常量 +10000）+ 0.6s 轨1（常量 -10000），顺序与区间列表一致
        self.assertEqual(len(samples), int(0.5 * RATE) + int(0.6 * RATE))
        self.assertTrue(all(value == 10_000 for value in samples[:8000]))
        self.assertTrue(all(value == -10_000 for value in samples[8000:]))

        report = self.assert_report(self.out_dir)
        self.assertAlmostEqual(report["output_duration_seconds"], 1.1, places=6)
        self.assertEqual(report["merged_interval_count"], 2)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["errors"], [])

    def test_named_flags_equivalent_to_positional(self):
        t0, t1 = self.make_tracks()
        intervals = self.make_intervals([{"track": 0, "start": 0.0, "end": 0.5}])
        result = run_cli(t0, t1, intervals, self.out_dir, use_flags=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.out_dir / "out.wav").is_file())


# ---------------------------------------------------------------------------
# 2. 输出采样率/位深跟随轨0，wave 可打开
# ---------------------------------------------------------------------------

class TestRequirement02(CLITestCase):
    def test_format_follows_track0(self):
        rate0 = 22_050
        t0 = write_wav(self.tmp / "track0.wav", constant(1000, 1.0, rate0), rate0)
        # 轨1 使用不同采样率，输出仍须跟随轨0
        t1 = write_wav(self.tmp / "track1.wav", constant(-1000, 1.0, 16_000), 16_000)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.0, "end": 0.5},
            {"track": 1, "start": 0.0, "end": 0.5},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        rate, width, channels, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(rate, rate0)
        self.assertEqual(width, 2)
        self.assertEqual(channels, 1)
        # 轨1 被重采样到轨0采样率，时长仍约 0.5s
        self.assertEqual(len(samples), int(0.5 * rate0) + int(0.5 * rate0))
        report = self.assert_report(self.out_dir)
        self.assertEqual(report["sample_rate"], rate0)
        self.assertEqual(report["bit_depth"], 16)


# ---------------------------------------------------------------------------
# 3. JSON 不可解析 / 缺必要字段：非零退出且无输出
# ---------------------------------------------------------------------------

class TestRequirement03(CLITestCase):
    def test_missing_required_field(self):
        t0, t1 = self.make_tracks()
        intervals = self.make_intervals([{"track": 0, "start": 0.1}])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("end", result.stderr)
        self.assertFalse((self.out_dir / "out.wav").exists())
        self.assertFalse((self.out_dir / "out_report.json").exists())

    def test_unparseable_json(self):
        t0, t1 = self.make_tracks()
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = run_cli(t0, t1, bad, self.out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("解析失败", result.stderr)
        self.assertFalse((self.out_dir / "out.wav").exists())

    def test_root_not_array(self):
        t0, t1 = self.make_tracks()
        bad = self.make_intervals({"foo": []})
        result = run_cli(t0, t1, bad, self.out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.out_dir / "out.wav").exists())


# ---------------------------------------------------------------------------
# 4. 按顺序拼接，总时长误差 <= 0.01s
# ---------------------------------------------------------------------------

class TestRequirement04(CLITestCase):
    def test_three_intervals_duration(self):
        t0, t1 = self.make_tracks(duration=5.0)
        spec = [
            {"track": 0, "start": 0.10, "end": 0.60},
            {"track": 1, "start": 1.00, "end": 1.75},
            {"track": 0, "start": 2.00, "end": 2.33},
        ]
        result = run_cli(t0, t1, self.make_intervals(spec), self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        report = self.assert_report(self.out_dir)
        expected = sum(item["end"] - item["start"] for item in spec)
        self.assertLessEqual(abs(report["output_duration_seconds"] - expected), 0.01)
        rate, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertLessEqual(abs(len(samples) / rate - expected), 0.01)
        self.assertEqual(report["merged_interval_count"], 3)


# ---------------------------------------------------------------------------
# 5. 越界截断并记录 warning
# ---------------------------------------------------------------------------

class TestRequirement05(CLITestCase):
    def test_end_past_track_is_truncated_with_warning(self):
        t0, t1 = self.make_tracks(duration=2.0)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.5, "end": 3.0},  # 超出末尾 1s
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        report = self.assert_report(self.out_dir)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("3", report["warnings"][0])
        self.assertIn("2.000", report["warnings"][0])
        self.assertEqual(report["errors"], [])
        self.assertAlmostEqual(report["output_duration_seconds"], 1.5, places=6)

    def test_negative_start_is_truncated_with_warning(self):
        t0, t1 = self.make_tracks(duration=2.0)
        intervals = self.make_intervals([
            {"track": 0, "start": -0.4, "end": 0.6},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_report(self.out_dir)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertAlmostEqual(report["output_duration_seconds"], 0.6, places=6)


# ---------------------------------------------------------------------------
# 6. 无效轨号 / 起止颠倒：条目级错误、跳过、不中断
# ---------------------------------------------------------------------------

class TestRequirement06(CLITestCase):
    def test_invalid_track_and_reversed_ranges(self):
        t0, t1 = self.make_tracks(duration=2.0)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.1, "end": 0.4, "fade": 0.0},
            {"track": 7, "start": 0.0, "end": 0.5},          # 无效轨号
            {"track": 1, "start": 0.2, "end": 0.5, "fade": 0.0},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        report = self.assert_report(self.out_dir)
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("轨号无效", report["errors"][0])
        self.assertEqual(report["merged_interval_count"], 2)
        self.assertAlmostEqual(report["output_duration_seconds"], 0.6, places=6)

        rate, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(len(samples), int(0.3 * rate) * 2)
        self.assertTrue(all(v == 10_000 for v in samples[:4800]))
        self.assertTrue(all(v == -10_000 for v in samples[4800:]))

    def test_reversed_range_is_skipped(self):
        t0, t1 = self.make_tracks()
        intervals = self.make_intervals([
            {"track": 0, "start": 1.0, "end": 0.5},
            {"track": 0, "start": 0.0, "end": 0.5},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_report(self.out_dir)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["merged_interval_count"], 1)


# ---------------------------------------------------------------------------
# 7. 某轨不可读：只跳过该轨条目，其他轨继续
# ---------------------------------------------------------------------------

class TestRequirement07(CLITestCase):
    def test_missing_track_file(self):
        t0, _ = self.make_tracks(duration=2.0)
        missing = self.tmp / "does_not_exist.wav"
        intervals = self.make_intervals([
            {"track": 0, "start": 0.2, "end": 0.8, "fade": 0.0},
            {"track": 1, "start": 0.0, "end": 0.5},
        ])
        result = run_cli(t0, missing, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        report = self.assert_report(self.out_dir)
        self.assertTrue(any("轨 1 读取失败" in message
                            for message in report["errors"]))
        self.assertEqual(report["merged_interval_count"], 1)
        self.assertAlmostEqual(report["output_duration_seconds"], 0.6, places=6)
        _, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertTrue(all(v == 10_000 for v in samples))


# ---------------------------------------------------------------------------
# 8. 全部条目无效：仍输出 0 秒 WAV 和非空报告
# ---------------------------------------------------------------------------

class TestRequirement08(CLITestCase):
    def test_all_entries_invalid(self):
        t0, t1 = self.make_tracks()
        intervals = self.make_intervals([
            {"track": 5, "start": 0.0, "end": 0.5},
            {"track": 0, "start": 0.9, "end": 0.2},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue((self.out_dir / "out.wav").is_file())
        rate, width, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(len(samples), 0)
        self.assertEqual(width, 2)

        report = self.assert_report(self.out_dir)
        self.assertEqual(report["output_duration_seconds"], 0.0)
        self.assertEqual(report["merged_interval_count"], 0)
        self.assertGreaterEqual(len(report["errors"]), 1)
        self.assertEqual(report["warnings"], [])


# ---------------------------------------------------------------------------
# 9. 淡化缺省、上限为区间一半、幅度单调递增
# ---------------------------------------------------------------------------

class TestRequirement09(CLITestCase):
    def test_fade_default_and_monotonic_amplitude(self):
        t0, t1 = self.make_tracks(track0_value=20_000, duration=1.5)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.2, "end": 0.7, "fade": 0.4},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        rate, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(len(samples), int(0.5 * rate))
        fade_frames = int(0.25 * rate)  # 0.4s 被压到区间一半 0.25s
        head = [abs(value) for value in samples[:fade_frames]]
        tail = [abs(value) for value in samples[-fade_frames:]]
        self.assertEqual(head[0], 0)
        self.assertEqual(head[-1], 20_000)
        self.assertTrue(all(a <= b for a, b in zip(head, head[1:])),
                        "区间头部幅度应单调递增")
        self.assertEqual(tail[0], 20_000)
        self.assertEqual(tail[-1], 0)
        self.assertTrue(all(a >= b for a, b in zip(tail, tail[1:])),
                        "区间尾部幅度应单调递减")
        # 中点前的样本振幅不超过满幅
        self.assertLessEqual(max(head), 20_000)

    def test_default_fade_is_0_01_seconds(self):
        t0, t1 = self.make_tracks(track0_value=20_000, duration=1.0)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.2, "end": 0.7},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        rate, _, _, samples = read_wav(self.out_dir / "out.wav")
        fade_frames = int(0.01 * rate)  # 160 帧
        self.assertEqual(abs(samples[fade_frames - 1]), 20_000)
        self.assertEqual(abs(samples[0]), 0)


# ---------------------------------------------------------------------------
# 10. 相邻淡化段重叠：无越界、拼接处平滑
# ---------------------------------------------------------------------------

class TestRequirement10(CLITestCase):
    def test_overlapping_fade_windows_are_safe(self):
        t0 = write_wav(self.tmp / "track0.wav", constant(32767, 1.0), RATE)
        t1 = write_wav(self.tmp / "track1.wav", constant(-32767, 1.0), RATE)
        # 两个区间在源时间上重叠 0.05s，各自淡化 0.1s
        intervals = self.make_intervals([
            {"track": 0, "start": 0.0, "end": 0.3, "fade": 0.1},
            {"track": 0, "start": 0.25, "end": 0.55, "fade": 0.1},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        report = self.assert_report(self.out_dir)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["merged_interval_count"], 2)

        rate, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertEqual(len(samples), int(0.3 * rate) * 2)
        # 全部样本在 16 位有符号范围内（array('h') 读取不会越界）
        self.assertTrue(all(-32768 <= value <= 32767 for value in samples))

        join = int(0.3 * rate)
        self.assertEqual(samples[join - 1], 0)
        self.assertEqual(samples[join], 0)
        max_close_delta = max(
            abs(samples[i] - samples[i - 1])
            for i in range(join - 100, join + 100))
        self.assertLessEqual(max_close_delta, 100,
                             "拼接处样本差值应平滑")
        # 淡化段内部相邻差值也受限，无跳变
        max_overall_delta = max(
            abs(samples[i] - samples[i - 1]) for i in range(1, len(samples)))
        self.assertLessEqual(max_overall_delta, 100)


# ---------------------------------------------------------------------------
# 11. 全幅方波输入，输出样本限幅在 16 位范围内
# ---------------------------------------------------------------------------

class TestRequirement11(CLITestCase):
    def test_full_scale_square_stays_in_int16(self):
        t0 = write_wav(self.tmp / "track0.wav", square(1.0), RATE)
        t1 = write_wav(self.tmp / "track1.wav", square(1.0), RATE)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.0, "end": 0.5, "fade": 0.05},
            {"track": 1, "start": 0.5, "end": 1.0, "fade": 0.05},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        _, _, _, samples = read_wav(self.out_dir / "out.wav")
        self.assertTrue(samples)
        self.assertTrue(all(-32768 <= value <= 32767 for value in samples))
        self.assertEqual(max(samples), 32767)
        self.assertEqual(min(samples), -32767)

    def test_limit_int16_helper_clamps(self):
        sys.path.insert(0, str(ROOT))
        import audio_merge  # noqa: WPS433
        values = array("h", [0, 1, -1, 32767, -32768])
        audio_merge.limit_int16(values)
        self.assertEqual(list(values), [0, 1, -1, 32767, -32768])

        widened = [40_000, -40_000, 32_768, -32_769]
        audio_merge.limit_int16(widened)
        self.assertEqual(widened, [32767, -32768, 32767, -32768])


# ---------------------------------------------------------------------------
# 12. 报告字段齐全，顺序记录，全有效时数组为空
# ---------------------------------------------------------------------------

class TestRequirement12(CLITestCase):
    def test_report_schema(self):
        t0, t1 = self.make_tracks()
        intervals = self.make_intervals([
            {"track": 0, "start": 0.0, "end": 0.5},
            {"track": 1, "start": 0.0, "end": 0.5},
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_report(self.out_dir)
        for key in ("output_duration_seconds", "merged_interval_count",
                    "warnings", "errors"):
            self.assertIn(key, report)
        self.assertIsInstance(report["warnings"], list)
        self.assertIsInstance(report["errors"], list)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["merged_interval_count"], 2)

    def test_warnings_and_errors_keep_input_order(self):
        t0, t1 = self.make_tracks(duration=2.0)
        intervals = self.make_intervals([
            {"track": 0, "start": 0.0, "end": 3.0},   # warning 1
            {"track": 9, "start": 0.0, "end": 0.5},   # error 1
            {"track": 0, "start": 0.0, "end": 2.5},   # warning 2
            {"track": 0, "start": 1.0, "end": 0.5},   # error 2
        ])
        result = run_cli(t0, t1, intervals, self.out_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.assert_report(self.out_dir)
        self.assertEqual(len(report["warnings"]), 2)
        self.assertEqual(len(report["errors"]), 2)
        self.assertIn("区间[0]", report["warnings"][0])
        self.assertIn("区间[2]", report["warnings"][1])
        self.assertIn("区间[1]", report["errors"][0])
        self.assertIn("区间[3]", report["errors"][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
