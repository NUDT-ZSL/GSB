#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_podcast_edl.py — 覆盖任务 10 条验收标准的单元测试。

运行: python -m unittest test_podcast_edl -v
样本全部由标准库 wave/json 在临时目录中确定性生成。
"""
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import podcast_edl as pe


def write_wav(path: Path, rate=16000, seconds=1.0, amp=20000,
              channels=1, width=2, constant=True):
    """确定性样本: constant=True 时为恒定幅值, 便于峰值断言。"""
    n = int(rate * seconds)
    if width == 2:
        frames = b"".join(struct.pack("<h", amp if constant else 0)
                          for _ in range(n * channels))
    else:
        frames = bytes([128 + min(amp, 100)] * (n * channels))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(frames)


def write_edl(path: Path, entries):
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def read_report(out_dir: Path):
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def read_output(out_dir: Path):
    with wave.open(str(out_dir / "output.wav"), "rb") as wf:
        rate = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
    samples = struct.unpack(f"<{n}h", raw)
    return rate, samples


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / "src"
        self.out = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def make_source(self, name="a.wav", **kw):
        p = self.src / name
        write_wav(p, **kw)
        return str(p)


# 验收1: 合法WAV成功打开; 立体声与8位各返回含"不支持的音频格式"的错误
class TestFormatValidation(Base):
    def test_valid_wav_opens(self):
        p = self.make_source(rate=16000)
        info = pe.read_source(p)
        self.assertEqual(info["rate"], 16000)
        self.assertAlmostEqual(info["duration"], 1.0)

    def test_valid_boundaries_8k_and_48k(self):
        for rate in (8000, 48000):
            info = pe.read_source(self.make_source(f"r{rate}.wav", rate=rate))
            self.assertEqual(info["rate"], rate)

    def test_stereo_rejected(self):
        p = self.make_source("stereo.wav", channels=2)
        with self.assertRaises(pe.SourceError) as cm:
            pe.read_source(p)
        self.assertIn("不支持的音频格式", str(cm.exception))

    def test_8bit_rejected(self):
        p = self.make_source("8bit.wav", width=1)
        with self.assertRaises(pe.SourceError) as cm:
            pe.read_source(p)
        self.assertIn("不支持的音频格式", str(cm.exception))


# 验收2: 有效清单解析全部条目; 缺字段时错误点名字段名
class TestEdlParsing(Base):
    def test_valid_edl_all_entries_parsed(self):
        src = self.make_source()
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 0.5, "normalize": False},
            {"source": src, "start": 0.5, "end": 1.0, "normalize": True},
        ])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 0)
        report = read_report(self.out)
        self.assertEqual(len(report["entries"]), 2)
        self.assertTrue(all(e["status"] == "success"
                            for e in report["entries"]))

    def test_missing_field_named_in_error(self):
        src = self.make_source()
        edl = self.root / "edl.json"
        write_edl(edl, [{"source": src, "start": 0.0, "normalize": False}])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 1)
        entry = read_report(self.out)["entries"][0]
        self.assertEqual(entry["status"], "error")
        self.assertIn("end", entry["error"])


# 验收3: 起始>=结束 跳过; 合法条目仍产出音频
class TestTimeRange(Base):
    def test_reversed_range_skipped_valid_still_produced(self):
        src = self.make_source(seconds=3.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 2.0, "end": 1.0, "normalize": False},
            {"source": src, "start": 0.0, "end": 1.0, "normalize": False},
        ])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 1)
        entries = read_report(self.out)["entries"]
        self.assertEqual(entries[0]["status"], "skipped")
        self.assertEqual(entries[1]["status"], "success")
        rate, samples = read_output(self.out)
        self.assertEqual(len(samples), 16000)  # 合法的一条产出了1秒音频

    def test_end_beyond_duration_skipped(self):
        src = self.make_source(seconds=1.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.5, "end": 2.0, "normalize": False}])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 1)
        self.assertEqual(read_report(self.out)["entries"][0]["status"],
                         "skipped")


# 验收4: 混用采样率整体拒绝, 错误含"采样率不一致"
class TestSampleRateMismatch(Base):
    def test_mixed_rates_rejected(self):
        s8 = self.make_source("a8.wav", rate=8000)
        s16 = self.make_source("a16.wav", rate=16000)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": s8, "start": 0.0, "end": 1.0, "normalize": False},
            {"source": s16, "start": 0.0, "end": 1.0, "normalize": False},
        ])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 1)
        report = read_report(self.out)
        self.assertTrue(all("采样率不一致" in (e["error"] or "")
                            for e in report["entries"]))
        self.assertFalse((self.out / "output.wav").exists())  # 不产出半成品


# 验收5: 归一化到29491; 不归一化保持原样
class TestNormalization(Base):
    def test_normalize_to_29491(self):
        src = self.make_source(seconds=2.0, amp=20000)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": True},
            {"source": src, "start": 1.0, "end": 2.0, "normalize": False},
        ])
        self.assertEqual(pe.run(str(edl), str(self.out)), 0)
        _, samples = read_output(self.out)
        first, second = samples[:16000], samples[16000:]
        self.assertEqual(max(abs(s) for s in first), 29491)
        self.assertEqual(max(abs(s) for s in second), 20000)


# 验收6: 两条各1秒拼接为2秒, 采样率与源一致
class TestConcatenation(Base):
    def test_two_one_second_clips_make_two_seconds(self):
        src = self.make_source(rate=16000, seconds=3.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": False},
            {"source": src, "start": 1.0, "end": 2.0, "normalize": False},
        ])
        self.assertEqual(pe.run(str(edl), str(self.out)), 0)
        rate, samples = read_output(self.out)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(samples), 2 * 16000)
        with wave.open(str(self.out / "output.wav"), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)


# 验收7: 报告逐条含序号/状态/错误信息/输出路径; success的error为null
class TestReport(Base):
    def test_report_fields(self):
        src = self.make_source(seconds=2.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": False},
            {"source": src, "start": 1.5, "end": 0.5, "normalize": False},
            {"source": str(self.src / "nope.wav"), "start": 0.0, "end": 1.0,
             "normalize": False},
        ])
        pe.run(str(edl), str(self.out))
        entries = read_report(self.out)["entries"]
        self.assertEqual([e["index"] for e in entries], [0, 1, 2])
        ok, skipped, err = entries
        self.assertEqual(ok["status"], "success")
        self.assertIsNone(ok["error"])
        self.assertTrue(ok["output"].endswith("output.wav"))
        self.assertEqual(skipped["status"], "skipped")
        self.assertTrue(skipped["error"])
        self.assertIsNone(skipped["output"])
        self.assertEqual(err["status"], "error")
        self.assertTrue(err["error"])


# 验收8: 输出目录自动创建; 同名文件覆盖写入
class TestOutputDir(Base):
    def test_dir_created_and_overwritten(self):
        src = self.make_source(seconds=3.0)
        out = self.root / "new" / "nested" / "dir"   # 不存在
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": False}])
        self.assertEqual(pe.run(str(edl), str(out)), 0)
        self.assertTrue((out / "output.wav").is_file())
        self.assertTrue((out / "report.json").is_file())
        _, first = read_output(out)
        # 修改清单后再次运行, 同名文件被覆盖为新结果
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 2.0, "normalize": False}])
        self.assertEqual(pe.run(str(edl), str(out)), 0)
        _, second = read_output(out)
        self.assertEqual(len(first), 16000)
        self.assertEqual(len(second), 32000)


# 验收9: 缺失/损坏源记error并说明原因, 其他条目继续完成
class TestSourceErrors(Base):
    def test_missing_and_corrupt_do_not_stop_others(self):
        good = self.make_source(seconds=2.0)
        corrupt = self.src / "corrupt.wav"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_bytes(b"garbage-not-wav")
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": str(self.src / "missing.wav"), "start": 0.0,
             "end": 1.0, "normalize": False},
            {"source": str(corrupt), "start": 0.0, "end": 1.0,
             "normalize": False},
            {"source": good, "start": 0.0, "end": 1.0, "normalize": False},
        ])
        code = pe.run(str(edl), str(self.out))
        self.assertEqual(code, 1)
        entries = read_report(self.out)["entries"]
        self.assertEqual([e["status"] for e in entries],
                         ["error", "error", "success"])
        self.assertIn("不存在", entries[0]["error"])
        self.assertIn("损坏", entries[1]["error"])
        _, samples = read_output(self.out)
        self.assertEqual(len(samples), 16000)  # 合法条目仍产出


# 验收10: 全部成功退出码0; 任一跳过/出错退出码1
class TestExitCode(Base):
    def test_all_success_exit_0(self):
        src = self.make_source(seconds=2.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": True},
            {"source": src, "start": 1.0, "end": 2.0, "normalize": False},
        ])
        self.assertEqual(pe.main([str(edl), str(self.out)]), 0)

    def test_out_of_range_exit_1(self):
        src = self.make_source(seconds=1.0)
        edl = self.root / "edl.json"
        write_edl(edl, [
            {"source": src, "start": 0.0, "end": 1.0, "normalize": False},
            {"source": src, "start": 0.0, "end": 5.0, "normalize": False},
        ])
        self.assertEqual(pe.main([str(edl), str(self.out)]), 1)


if __name__ == "__main__":
    unittest.main()
