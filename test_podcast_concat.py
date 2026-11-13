#!/usr/bin/env python3
"""test_podcast_concat.py — 对 podcast_concat.py 的 15 项验收标准逐一验证。

运行: python test_podcast_concat.py -v
所有样本均由代码确定性生成,仅使用标准库。
"""

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "podcast_concat.py")
RATE = 8000  # 测试统一使用 8000Hz,样本量小、速度快


# ------------------------------------------------------------ 样本构造工具

def tone(seconds, amp=10000, freq=440.0, rate=RATE):
    n = int(rate * seconds)
    return [int(amp * math.sin(2.0 * math.pi * freq * i / rate))
            for i in range(n)]


def dc(seconds, value=10000, rate=RATE):
    return [value] * int(rate * seconds)


def silence(seconds, rate=RATE):
    return [0] * int(rate * seconds)


def write_wav(path, samples, rate=RATE, channels=1, sampwidth=2):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        if sampwidth == 2:
            wf.writeframes(struct.pack("<%dh" % len(samples), *samples))
        else:
            wf.writeframes(bytes(samples))


def write_float32_wav(path, rate=RATE, frames=800):
    """手工构造 32 位 IEEE 浮点 WAV(format tag = 3)。"""
    data = b"\x00" * (frames * 4)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 4 + 24 + 8 + len(data)) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<I", 16) + fmt)
        fh.write(b"data" + struct.pack("<I", len(data)) + data)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def read_wav_frames(path):
    with wave.open(path, "rb") as wf:
        params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate(),
                  wf.getnframes())
        raw = wf.readframes(params[3])
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw) if raw else ()
    return params, samples


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, TOOL, *args],
            cwd=self.dir, capture_output=True, text=True)

    def make_wav(self, samples, name="in.wav", rate=RATE):
        path = os.path.join(self.dir, name)
        write_wav(path, samples, rate=rate)
        return name

    def make_manifest(self, clips, name="clips.json"):
        path = os.path.join(self.dir, name)
        write_json(path, clips)
        return name

    def read_report(self, output="output.wav"):
        stem, _ = os.path.splitext(output)
        with open(os.path.join(self.dir, stem + ".report.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def output_files(self):
        return [f for f in os.listdir(self.dir)
                if f.endswith(".wav") or f.endswith(".report.json")]


# ------------------------------------------------------------ 1. WAV 读取与限制

class TestWavReading(ToolCase):
    def test_valid_wav_reads_rate_and_duration(self):
        self.make_wav(tone(2.0))
        self.make_manifest([{"name": "a", "start": 0.0, "end": 1.0}])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = self.read_report()
        self.assertEqual(report["sample_rate"], RATE)
        self.assertAlmostEqual(report["duration"], 2.0, places=3)

    def test_oversize_file_rejected(self):
        big = os.path.join(self.dir, "big.wav")
        with open(big, "wb") as fh:
            fh.truncate(10 * 1024 * 1024 + 1)
        self.make_manifest([])
        r = self.run_tool("--input", "big.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("10MB", r.stderr)

    def test_sample_rate_out_of_range_rejected(self):
        for bad_rate in (4000, 96000):
            sub = tempfile.TemporaryDirectory()
            self.addCleanup(sub.cleanup)
            write_wav(os.path.join(sub.name, "x.wav"), tone(0.5, rate=bad_rate),
                      rate=bad_rate)
            write_json(os.path.join(sub.name, "c.json"), [])
            r = subprocess.run(
                [sys.executable, TOOL, "--input", "x.wav", "--manifest", "c.json"],
                cwd=sub.name, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, f"rate={bad_rate}")
            self.assertIn("采样率", r.stderr)


# ------------------------------------------------------------ 2. 停顿检测精度

class TestPauseDetection(ToolCase):
    def test_two_second_pause_detected_within_10ms(self):
        samples = tone(2.0) + silence(2.0) + tone(2.0)  # 停顿 2.0~4.0
        self.make_wav(samples)
        self.make_manifest([])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        pauses = self.read_report()["pauses"]
        self.assertEqual(len(pauses), 1)
        self.assertAlmostEqual(pauses[0]["start"], 2.0, delta=0.01)
        self.assertAlmostEqual(pauses[0]["end"], 4.0, delta=0.01)

    def test_pauses_sorted_by_start(self):
        samples = (tone(1.0) + silence(1.0) + tone(1.0) +
                   silence(1.0) + tone(1.0))
        self.make_wav(samples)
        self.make_manifest([])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        starts = [p["start"] for p in self.read_report()["pauses"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), 2)


# ------------------------------------------------------------ 3. 阈值参数

class TestThreshold(ToolCase):
    def make_threshold_wav(self):
        # 1 秒正常 + 1 秒 -44dBFS 低音量 + 1 秒正常
        samples = tone(1.0) + tone(1.0, amp=200) + tone(1.0)
        self.make_wav(samples)
        self.make_manifest([])

    def test_threshold_changes_pause_list(self):
        self.make_threshold_wav()
        r1 = self.run_tool("--input", "in.wav", "--manifest", "clips.json",
                           "--output", "a.wav")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        default_pauses = self.read_report("a.wav")["pauses"]
        r2 = self.run_tool("--input", "in.wav", "--manifest", "clips.json",
                           "--output", "b.wav", "--threshold", "-50")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        strict_pauses = self.read_report("b.wav")["pauses"]
        self.assertNotEqual(default_pauses, strict_pauses)
        self.assertEqual(len(default_pauses), 1)   # -40dBFS 下低音量段记为停顿
        self.assertEqual(len(strict_pauses), 0)    # -50dBFS 下不记

    def test_threshold_out_of_range_rejected(self):
        self.make_threshold_wav()
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json",
                          "--threshold", "-70")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("阈值", r.stderr)


# ------------------------------------------------------------ 4. 清单字段校验

class TestManifestValidation(ToolCase):
    def setUp(self):
        super().setUp()
        self.make_wav(tone(3.0))

    def test_start_not_less_than_end_rejected(self):
        self.make_manifest([{"name": "bad", "start": 2.0, "end": 1.0}])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("start", r.stderr)
        self.assertIn("end", r.stderr)

    def test_missing_field_rejected_with_field_name(self):
        self.make_manifest([{"name": "bad", "start": 0.0}])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("end", r.stderr)


# ------------------------------------------------------------ 5/6/7. 修剪、拼接、报告

class TestConcatAndTrim(ToolCase):
    def make_pause_wav(self):
        # 讲话 0~2,停顿 2~4,讲话 4~6
        samples = tone(2.0) + silence(2.0) + tone(2.0)
        self.make_wav(samples)

    def test_overlap_over_100ms_trimmed_with_warning(self):
        self.make_pause_wav()
        self.make_manifest([
            {"name": "overlap", "start": 1.0, "end": 2.2},  # 与停顿重叠 0.2 秒
            {"name": "clean", "start": 4.5, "end": 5.5},
        ])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = self.read_report()
        seg = report["segments"][0]
        self.assertEqual(seg["output_end"] - seg["output_start"], 1.0)  # 1.2 -> 1.0
        self.assertTrue(seg["warnings"])
        self.assertIn("停顿", seg["warnings"][0])
        params, out = read_wav_frames(os.path.join(self.dir, "output.wav"))
        self.assertEqual(params[3], 2 * RATE)  # 1.0s + 1.0s

    def test_small_overlap_not_trimmed(self):
        self.make_pause_wav()
        self.make_manifest([{"name": "tiny", "start": 1.0, "end": 2.05}])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        seg = self.read_report()["segments"][0]
        self.assertFalse(seg["warnings"])
        self.assertAlmostEqual(seg["output_end"] - seg["output_start"], 1.05,
                               places=3)

    def test_three_1s_clips_concat_to_3s_with_fade(self):
        samples = dc(4.0)  # 恒定幅度,便于观察渐变
        self.make_wav(samples)
        self.make_manifest([
            {"name": "c1", "start": 0.0, "end": 1.0},
            {"name": "c2", "start": 1.0, "end": 2.0},
            {"name": "c3", "start": 2.0, "end": 3.0},
        ])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        params, out = read_wav_frames(os.path.join(self.dir, "output.wav"))
        self.assertEqual(params[0], 1)          # 单声道
        self.assertEqual(params[1], 2)          # 16 位
        self.assertEqual(params[2], RATE)       # 采样率保持
        self.assertAlmostEqual(params[3] / RATE, 3.0, places=3)
        # 第二段起点(样本 8000)处幅度从 0 渐增:线性渐变
        boundary = RATE
        self.assertEqual(out[boundary], 0)
        self.assertLess(out[boundary + 1], out[boundary + 40])
        self.assertLess(out[boundary + 40], out[boundary + 79])
        # 第一段结尾(样本 7999)渐出到 0
        self.assertEqual(out[RATE - 1], 0)
        self.assertGreater(out[RATE - 80], out[RATE - 2])

    def test_report_fields_order_and_decimals(self):
        self.make_pause_wav()
        names = ["z-first", "a-second", "m-third"]
        self.make_manifest([
            {"name": names[0], "start": 0.0, "end": 1.234},
            {"name": names[1], "start": 1.0, "end": 2.2},
            {"name": names[2], "start": 4.0, "end": 5.0},
        ])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = self.read_report()
        segs = report["segments"]
        self.assertEqual([s["name"] for s in segs], names)  # 顺序与输入一致
        for seg in segs:
            for field in ("name", "start", "end", "output_start",
                          "output_end", "warnings"):
                self.assertIn(field, seg)
        # 所有时间字段小数位不超过三位
        def check(v):
            self.assertEqual(v, round(v, 3))
        for seg in segs:
            check(seg["start"]); check(seg["end"])
            if seg["output_start"] is not None:
                check(seg["output_start"]); check(seg["output_end"])
        for p in report["pauses"]:
            check(p["start"]); check(p["end"])


# ------------------------------------------------------------ 8. 空清单

class TestEmptyManifest(ToolCase):
    def test_empty_manifest_produces_zero_length_wav(self):
        self.make_wav(tone(2.0))
        self.make_manifest([])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        params, _ = read_wav_frames(os.path.join(self.dir, "output.wav"))
        self.assertEqual(params[3], 0)  # 0 秒但为合法 WAV
        self.assertEqual(params[2], RATE)
        self.assertEqual(self.read_report()["segments"], [])


# ------------------------------------------------------------ 9. 格式拒绝

class TestFormatRejection(ToolCase):
    def test_float32_wav_rejected(self):
        write_float32_wav(os.path.join(self.dir, "float.wav"))
        self.make_manifest([])
        r = self.run_tool("--input", "float.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("16 位单声道 PCM", r.stderr)

    def test_stereo_wav_rejected(self):
        stereo = [v for s in tone(0.5) for v in (s, s)]
        write_wav(os.path.join(self.dir, "stereo.wav"), stereo, channels=2)
        self.make_manifest([])
        r = self.run_tool("--input", "stereo.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("16 位单声道 PCM", r.stderr)


# ------------------------------------------------------------ 10. 输出已存在

class TestOutputExists(ToolCase):
    def test_existing_output_refused_and_unchanged(self):
        self.make_wav(tone(1.0))
        self.make_manifest([])
        original = b"ORIGINAL-CONTENT-DO-NOT-TOUCH"
        with open(os.path.join(self.dir, "output.wav"), "wb") as fh:
            fh.write(original)
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("已存在", r.stderr)
        with open(os.path.join(self.dir, "output.wav"), "rb") as fh:
            self.assertEqual(fh.read(), original)  # 原文件内容不变


# ------------------------------------------------------------ 11. 损坏输入

class TestCorruptInput(ToolCase):
    def test_random_bytes_rejected_without_partial_output(self):
        with open(os.path.join(self.dir, "junk.wav"), "wb") as fh:
            fh.write(bytes(range(256)) * 40)
        self.make_manifest([])
        before = set(os.listdir(self.dir))
        r = self.run_tool("--input", "junk.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("WAV", r.stderr)
        after = set(os.listdir(self.dir))
        self.assertEqual(before, after)  # 目录中无新输出文件


# ------------------------------------------------------------ 12. 命令行参数

class TestCli(ToolCase):
    def test_help_lists_four_options(self):
        r = subprocess.run([sys.executable, TOOL, "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for opt in ("--input", "--manifest", "--output", "--threshold"):
            self.assertIn(opt, r.stdout)

    def test_missing_manifest_errors(self):
        self.make_wav(tone(1.0))
        r = self.run_tool("--input", "in.wav")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--manifest", r.stderr)


# ------------------------------------------------------------ 13. 确定性

class TestDeterminism(ToolCase):
    def test_repeated_runs_byte_identical(self):
        hashes = []
        for tag in ("run1", "run2"):
            sub = os.path.join(self.dir, tag)
            os.makedirs(sub)
            samples = tone(2.0) + silence(1.0) + tone(2.0)
            write_wav(os.path.join(sub, "in.wav"), samples)
            write_json(os.path.join(sub, "clips.json"),
                       [{"name": "a", "start": 0.0, "end": 1.5},
                        {"name": "b", "start": 3.5, "end": 4.5}])
            r = subprocess.run(
                [sys.executable, TOOL, "--input", "in.wav",
                 "--manifest", "clips.json"],
                cwd=sub, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            hashes.append((sha256(os.path.join(sub, "output.wav")),
                           sha256(os.path.join(sub, "output.report.json"))))
        self.assertEqual(hashes[0], hashes[1])  # WAV 与报告哈希均一致


# ------------------------------------------------------------ 14. 剔除零长度片段

class TestDropClip(ToolCase):
    def test_clip_fully_inside_pause_dropped(self):
        samples = tone(2.0) + silence(2.0) + tone(2.0)  # 停顿 2~4
        self.make_wav(samples)
        self.make_manifest([
            {"name": "keep", "start": 0.0, "end": 1.0},
            {"name": "lost", "start": 2.5, "end": 3.5},  # 整段落在停顿内
        ])
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = self.read_report()
        lost = report["segments"][1]
        self.assertTrue(lost["dropped"])
        self.assertIsNone(lost["output_start"])
        self.assertTrue(any("剔除" in w for w in lost["warnings"]))
        params, _ = read_wav_frames(os.path.join(self.dir, "output.wav"))
        self.assertEqual(params[3], RATE)  # 输出仅含 keep 的 1 秒


# ------------------------------------------------------------ 15. 越界清单

class TestOutOfBounds(ToolCase):
    def test_end_beyond_duration_rejects_whole_manifest(self):
        self.make_wav(tone(3.0))  # 时长 3 秒
        self.make_manifest([
            {"name": "ok", "start": 0.0, "end": 1.0},
            {"name": "too-far", "start": 1.0, "end": 4.0},  # 超出 1 秒
        ])
        before = set(os.listdir(self.dir))
        r = self.run_tool("--input", "in.wav", "--manifest", "clips.json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("too-far", r.stderr)  # 提示越界片段名
        self.assertEqual(set(os.listdir(self.dir)), before)  # 不写出任何输出


if __name__ == "__main__":
    unittest.main(verbosity=2)
