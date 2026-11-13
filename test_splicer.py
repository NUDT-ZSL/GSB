# -*- coding: utf-8 -*-
"""拼接器验收测试：覆盖任务 14 条验收点。运行: python -m unittest test_splicer -v"""
import json
import os
import struct
import tempfile
import unittest
import wave

import splicer
from make_samples import write_wav, RATE


def read_final(path):
    with wave.open(path, "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        raw = w.readframes(w.getnframes())
    return params, list(struct.unpack("<%dh" % (len(raw) // 2), raw))


class SplicerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def wav(self, name, samples, **kw):
        path = os.path.join(self.dir, name)
        write_wav(path, samples, **kw)
        return path

    def out(self, name="final.wav"):
        return os.path.join(self.dir, name)


class TestConcatAndFormat(SplicerTestBase):
    """验收1/13: 顺序拼接，采样数等于各段裁剪后之和；文件头固定 44100/单声道/16位。"""

    def test_concat_sample_count_and_header(self):
        a = self.wav("a.wav", list(range(1, 5001)))            # 5000 样本
        b = self.wav("b.wav", [100] * 3000)                    # 3000 样本
        manifest = {"output": self.out(), "segments": [
            {"file": a, "start_ms": 0.0, "end_ms": 2000 / RATE * 1000},   # 裁剪到 2000
            {"file": b},
        ]}
        report = splicer.run(manifest)
        params, samples = read_final(self.out())
        self.assertEqual(params, (1, 2, 44100))
        self.assertEqual(len(samples), 2000 + 3000)
        self.assertEqual(report["summary"]["processed"], 2)
        # 顺序: 前 2000 个来自 a(递增)，之后来自 b(恒定)
        self.assertLess(samples[0], samples[1999])
        self.assertEqual(samples[2000], samples[-1])


class TestReverse(SplicerTestBase):
    """验收2: 倒放严格递减，报告两段时长相等。"""

    def test_reverse_strictly_decreasing_and_durations_equal(self):
        ramp = self.wav("ramp.wav", list(range(1, 22051)))     # 严格递增
        report = splicer.run({"output": self.out(), "segments": [
            {"file": ramp, "reverse": True}]})
        _, samples = read_final(self.out())
        self.assertTrue(all(samples[i] > samples[i + 1]
                            for i in range(len(samples) - 1)), "倒放后应严格递减")
        seg = report["segments"][0]
        self.assertEqual(seg["original_duration_ms"], seg["processed_duration_ms"])


class TestSilenceTrim(SplicerTestBase):
    """验收3: 去首尾静音，绝对值等于阈值视为静音。"""

    def test_trim_length_equals_non_silent_region(self):
        # 阈值 100: 首尾 0 与 ±100 均为静音，非静音区间 = 2000 个 5000
        data = [0] * 1000 + [5000] * 2000 + [100, -100] + [0] * 500
        f = self.wav("p.wav", data)
        report = splicer.run({"output": self.out(), "segments": [
            {"file": f, "silence_threshold": 100}]})
        seg = report["segments"][0]
        self.assertEqual(seg["processed_duration_ms"], 2000 / RATE * 1000.0)
        _, samples = read_final(self.out())
        self.assertEqual(len(samples), 2000)


class TestGainClamp(SplicerTestBase):
    """验收4: 增益翻倍，超限钳位到 32767 / -32768。"""

    def test_gain_and_clamp(self):
        f = self.wav("g.wav", [10000, -10000, 20000, -20000])
        splicer.run({"output": self.out(), "segments": [{"file": f, "gain": 2.0}]})
        _, samples = read_final(self.out())
        # 归一化前验证: 直接调用片段处理
        processed = splicer.process_segment_samples([10000, -10000, 20000, -20000],
                                                    {"gain": 2.0})
        self.assertEqual(processed, [20000, -20000, 32767, -32768])
        self.assertLessEqual(max(samples), 32767)
        self.assertGreaterEqual(min(samples), -32768)


class TestNormalize(SplicerTestBase):
    """验收5: 归一化到满量程 90% = 29491；全零保持全零。"""

    def test_normalize_peak_29491(self):
        f = self.wav("n.wav", [1000, -500, 250])
        splicer.run({"output": self.out(), "segments": [{"file": f}]})
        _, samples = read_final(self.out())
        self.assertEqual(max(abs(s) for s in samples), 29491)
        self.assertTrue(all(-32768 <= s <= 32767 for s in samples))

    def test_all_zero_stays_zero(self):
        f = self.wav("z.wav", [0] * 1000)
        report = splicer.run({"output": self.out(), "segments": [{"file": f}]})
        _, samples = read_final(self.out())
        self.assertEqual(samples, [0] * 1000)
        self.assertTrue(report["success"])


class TestInvalidFiles(SplicerTestBase):
    """验收6: 缺失/立体声/48k 被跳过且原因正确。"""

    def test_skip_reasons(self):
        stereo = self.wav("st.wav", [1, -1] * 500, nch=2)
        rate48 = self.wav("r48.wav", [1] * 4800, rate=48000)
        ok = self.wav("ok.wav", [100] * 1000)
        missing = os.path.join(self.dir, "nope.wav")
        report = splicer.run({"output": self.out(), "segments": [
            {"file": missing}, {"file": stereo}, {"file": rate48}, {"file": ok}]})
        segs = report["segments"]
        self.assertEqual([s["status"] for s in segs],
                         ["skipped", "skipped", "skipped", "processed"])
        self.assertIn("文件缺失", segs[0]["reason"])
        self.assertIn("非单声道", segs[1]["reason"])
        self.assertIn("采样率非44100", segs[2]["reason"])
        self.assertIsNone(segs[3]["reason"])


class TestTimeBounds(SplicerTestBase):
    """验收7: 越界与倒置起止时间被跳过并记录原因。"""

    def test_out_of_range_and_inverted(self):
        f = self.wav("t.wav", [1] * 4410)  # 100ms
        report = splicer.run({"output": self.out(), "segments": [
            {"file": f, "start_ms": 0.0, "end_ms": 500.0},
            {"file": f, "start_ms": 80.0, "end_ms": 20.0},
            {"file": f, "start_ms": -5.0},
        ]})
        segs = report["segments"]
        self.assertTrue(all(s["status"] == "skipped" for s in segs))
        self.assertIn("越界", segs[0]["reason"])
        self.assertIn("倒置", segs[1]["reason"])
        self.assertIn("越界", segs[2]["reason"])
        self.assertFalse(report["success"])


class TestFieldValidation(SplicerTestBase):
    """验收8: 非法字段类型阻止处理，错误信息含字段路径。"""

    def test_reverse_string_and_threshold_string(self):
        f = self.wav("v.wav", [1] * 100)
        with self.assertRaises(splicer.ManifestError) as ctx:
            splicer.run({"segments": [{"file": f}, {"file": f, "reverse": "yes"}]})
        self.assertIn("segments[1].reverse", str(ctx.exception))
        with self.assertRaises(splicer.ManifestError) as ctx2:
            splicer.run({"segments": [{"file": f, "silence_threshold": "abc"}]})
        self.assertIn("segments[0].silence_threshold", str(ctx2.exception))


class TestReportStructure(SplicerTestBase):
    """验收9/10/14: 逐段字段齐全、汇总一致、UTF-8 JSON 毫秒浮点。"""

    def test_mixed_manifest_report(self):
        a = self.wav("a.wav", list(range(1, 2001)))
        b = self.wav("b.wav", [0] * 100 + [3000] * 500 + [0] * 100)
        missing = os.path.join(self.dir, "gone.wav")
        report_path = os.path.join(self.dir, "report.json")
        manifest = {"output": self.out(), "report": report_path, "segments": [
            {"file": a, "reverse": True},
            {"file": b, "silence_threshold": 0},
            {"file": missing},
        ]}
        report = splicer.run(manifest)
        # 逐段字段
        for seg in report["segments"]:
            for key in ("index", "status", "reason",
                        "original_duration_ms", "processed_duration_ms"):
                self.assertIn(key, seg)
        self.assertEqual(report["segments"][0]["status"], "processed")
        self.assertIsNone(report["segments"][0]["reason"])
        self.assertEqual(report["segments"][2]["status"], "skipped")
        self.assertIsNotNone(report["segments"][2]["reason"])
        # 汇总与逐段一致
        s = report["summary"]
        self.assertEqual(s["total_segments"], 3)
        self.assertEqual(s["processed"],
                         sum(1 for x in report["segments"] if x["status"] == "processed"))
        self.assertEqual(s["skipped"],
                         sum(1 for x in report["segments"] if x["status"] == "skipped"))
        self.assertEqual(s["output_path"], report["output_path"])
        _, samples = read_final(self.out())
        self.assertAlmostEqual(s["total_duration_ms"], len(samples) / RATE * 1000.0)
        # UTF-8 JSON 可解析，时间为毫秒浮点
        with open(report_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False)
        with open(report_path, "r", encoding="utf-8") as fp:
            loaded = json.load(fp)
        self.assertIsInstance(loaded["segments"][0]["original_duration_ms"], float)
        self.assertIsInstance(loaded["summary"]["total_duration_ms"], float)
        self.assertIsNone(loaded["segments"][0]["reason"])
        self.assertTrue(any("文件缺失" in (x["reason"] or "") for x in loaded["segments"]))


class TestAllInvalid(SplicerTestBase):
    """验收11: 全部无效时不生成成品，报告整体失败并列出原因。"""

    def test_all_invalid_no_output(self):
        stereo = self.wav("st.wav", [1, -1] * 100, nch=2)
        out = self.out("never.wav")
        report = splicer.run({"output": out, "segments": [
            {"file": os.path.join(self.dir, "m.wav")},
            {"file": stereo},
        ]})
        self.assertFalse(os.path.exists(out))
        self.assertFalse(report["success"])
        self.assertIsNone(report["output_path"])
        self.assertIn("所有片段均无效", report["error"])
        self.assertEqual(len(report["reasons"]), 2)
        self.assertTrue(all(r for r in report["reasons"]))


class TestTransientReadFault(SplicerTestBase):
    """验收12: 瞬时读取故障重试后恢复，其余段不受影响。"""

    def test_retry_recovers(self):
        a = self.wav("a.wav", [500] * 1000)
        b = self.wav("b.wav", [700] * 800)
        original = splicer.read_wav_samples
        state = {"calls": 0}

        def flaky(path):
            if path == a and state["calls"] == 0:
                state["calls"] += 1
                raise OSError("暂时性设备繁忙")
            return original(path)

        splicer.read_wav_samples = flaky
        try:
            report = splicer.run({"output": self.out(), "segments": [
                {"file": a}, {"file": b}]})
        finally:
            splicer.read_wav_samples = original
        self.assertEqual([s["status"] for s in report["segments"]],
                         ["processed", "processed"])
        _, samples = read_final(self.out())
        self.assertEqual(len(samples), 1800)

    def test_persistent_fault_skipped_others_ok(self):
        a = self.wav("a.wav", [500] * 1000)
        b = self.wav("b.wav", [700] * 800)
        original = splicer.read_wav_samples

        def broken(path):
            if path == a:
                raise OSError("持续故障")
            return original(path)

        splicer.read_wav_samples = broken
        try:
            report = splicer.run({"output": self.out(), "segments": [
                {"file": a}, {"file": b}]})
        finally:
            splicer.read_wav_samples = original
        self.assertEqual(report["segments"][0]["status"], "skipped")
        self.assertIn("读取失败", report["segments"][0]["reason"])
        self.assertEqual(report["segments"][1]["status"], "processed")
        _, samples = read_final(self.out())
        self.assertEqual(len(samples), 800)


class TestCliEndToEnd(SplicerTestBase):
    """端到端: 生成样本 -> 运行清单 -> 校验产物。"""

    def test_demo_manifest(self):
        import make_samples
        ws = make_samples.build(os.path.join(self.dir, "ws"))
        report = splicer.run_file(os.path.join(ws, "manifest_demo.json"))
        self.assertTrue(report["success"])
        self.assertEqual(report["summary"]["processed"], 3)
        self.assertEqual(report["summary"]["skipped"], 5)
        params, samples = read_final(report["output_path"])
        self.assertEqual(params, (1, 2, 44100))
        self.assertEqual(max(abs(s) for s in samples), 29491)
        with open(os.path.join(ws, "report.json"), encoding="utf-8") as f:
            json.load(f)


if __name__ == "__main__":
    unittest.main()
