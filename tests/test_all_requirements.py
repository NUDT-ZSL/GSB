"""10 项验收要求的单元 / 集成测试（仅依赖标准库 unittest + Pillow）。

运行::

    python -m unittest -v tests.test_all_requirements
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
import wave

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from beat_align.align import (  # noqa: E402
    DIRECTION_EARLIER,
    DIRECTION_LATER,
    DIRECTION_NONE,
    align_shots,
    nearest_beat,
)
from beat_align.audio import load_wav  # noqa: E402
from beat_align.detect import detect_beats  # noqa: E402
from beat_align.errors import (  # noqa: E402
    AudioReadError,
    OutputNotWritableError,
    UnsupportedAudioError,
)
from beat_align.pipeline import run_pipeline  # noqa: E402
from beat_align.preview import format_timecode, render_all  # noqa: E402
from beat_align.report import write_report  # noqa: E402
from beat_align.shots import load_shots, mark_overhang  # noqa: E402

SR = 16000


def _write_wav(path, samples, channels=1, sample_width=2, rate=SR):
    with wave.open(path, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        if sample_width == 2:
            if channels == 2:
                interleaved = [v for pair in zip(samples, samples) for v in pair]
                wav.writeframes(struct.pack(f"<{len(interleaved)}h", *interleaved))
            else:
                wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        elif sample_width == 1:
            unsigned = [max(0, min(255, (v // 256) + 128)) for v in samples]
            wav.writeframes(bytes(unsigned))
        elif sample_width == 4:
            wav.writeframes(struct.pack(f"<{len(samples)}i", *[v * 65536 for v in samples]))
        elif sample_width == 3:
            raw = bytearray()
            for v in samples:
                v3 = max(-(1 << 23), min((1 << 23) - 1, int(v) * 256))
                raw += v3.to_bytes(3, "little", signed=True)
            wav.writeframes(bytes(raw))


def _synth_drum_track(beat_times_ms, duration_ms=10_000, rate=SR):
    """合成鼓点音频，起振样本与给定毫秒严格对齐。"""
    total = rate * duration_ms // 1000
    samples = [0] * total
    for beat_ms in beat_times_ms:
        start = rate * beat_ms // 1000
        for k in range(rate // 4):  # 250 ms 鼓身
            idx = start + k
            if idx >= total:
                break
            t = k / rate
            samples[idx] += int(28000 * math.exp(-t * 18.0) * math.sin(2 * math.pi * 60 * t))
        for k in range(rate // 25):  # 40 ms 高频瞬态
            idx = start + k
            if idx >= total:
                break
            t = k / rate
            samples[idx] += int(16000 * math.exp(-t * 90.0) * math.sin(2 * math.pi * 2500 * t))
    return [max(-32768, min(32767, v)) for v in samples]


def _write_shots(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.wav = os.path.join(self.dir, "music.wav")
        self.shots = os.path.join(self.dir, "shots.json")
        self.out = os.path.join(self.dir, "previews")
        self.report = os.path.join(self.dir, "report.json")
        _write_wav(self.wav, _synth_drum_track(list(range(0, 10_000, 1000))))

    def tearDown(self):
        self.tmp.cleanup()

    # --- 需求 1：WAV 校验 --------------------------------------------------
    def test_01_mono_wav_loads_stereo_rejected(self):
        info = load_wav(self.wav)
        self.assertEqual(info.sample_rate, SR)
        self.assertEqual(info.bits_per_sample, 16)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.duration_ms, 10_000)

        stereo = os.path.join(self.dir, "stereo.wav")
        _write_wav(stereo, [100] * SR, channels=2)
        with self.assertRaises(UnsupportedAudioError) as ctx:
            load_wav(stereo)
        self.assertIn("2 声道", str(ctx.exception))
        self.assertIn("单声道", str(ctx.exception))

    def test_01b_other_formats_and_depths(self):
        for width, bits in ((1, 8), (3, 24), (4, 32)):
            path = os.path.join(self.dir, f"d{bits}.wav")
            _write_wav(path, [1000] * SR, sample_width=width)
            self.assertEqual(load_wav(path).bits_per_sample, bits)

        bad_rate = os.path.join(self.dir, "lowrate.wav")
        _write_wav(bad_rate, [100] * 800, rate=4000)
        with self.assertRaises(UnsupportedAudioError) as ctx:
            load_wav(bad_rate)
        self.assertIn("采样率", str(ctx.exception))

    # --- 需求 2：鼓点检出 + 合并 -------------------------------------------
    def test_02_beat_detection_accuracy_and_merge(self):
        expected = list(range(0, 10_000, 1000))
        info = load_wav(self.wav)
        beats = detect_beats(info)
        got = [b.time_ms for b in beats]
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), len(expected))
        # 与预期相差不超过一帧（hop_ms=10ms）
        for actual, want in zip(got, expected):
            self.assertLessEqual(abs(actual - want), 10, f"鼓点 {want}ms 检出偏差过大: {actual}")

        # 双倍密度鼓点：500ms 处的"重复检出"与 0ms 鼓点相距过近，必须合并
        dense_beats = [0, 500, 1000, 2000, 3000]
        dense_wav = os.path.join(self.dir, "dense.wav")
        _write_wav(dense_wav, _synth_drum_track(dense_beats, duration_ms=4000))
        dense = detect_beats(load_wav(dense_wav), min_gap_ms=120)
        times = [b.time_ms for b in dense]
        # 0 与 500 相距 500ms > min_gap，此处验证相邻间隔均不小于 min_gap
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 120)
        # 直接构造两个相隔 30ms 的鼓点，验证合并为一个节奏点
        close_wav = os.path.join(self.dir, "close.wav")
        _write_wav(close_wav, _synth_drum_track([1000, 1030, 3000], duration_ms=4000))
        close = detect_beats(load_wav(close_wav), min_gap_ms=120)
        close_times = [b.time_ms for b in close]
        self.assertEqual(len(close_times), 2)
        self.assertLessEqual(abs(close_times[0] - 1000), 10)

    # --- 需求 3：清单累计起点 + 超限标记 -----------------------------------
    def test_03_cumulative_starts_and_overhang(self):
        overhang_wav = os.path.join(self.dir, "ov.wav")
        _write_wav(overhang_wav, _synth_drum_track([0, 1000, 2000], duration_ms=10_000))
        _write_shots(self.shots, [
            {"title": "A", "duration_ms": 3000},
            {"title": "B", "duration_ms": 4000},
            {"title": "C", "duration_ms": 4000},  # 总长 11000，超配乐 1000ms
        ])
        entries = load_shots(self.shots)
        self.assertEqual([e.original_start_ms for e in entries], [0, 3000, 7000])
        info = load_wav(overhang_wav)
        flag = mark_overhang(entries, info.duration_ms)
        self.assertTrue(flag["total_exceeds_audio"])
        self.assertEqual(flag["overhang_ms"], 1000)
        # 超限镜头仍参与对齐
        aligned = align_shots(entries, detect_beats(info))
        self.assertEqual(len(aligned), 3)

    # --- 需求 4：最近鼓点对齐、偏移量与方向 --------------------------------
    def test_04_align_offset_and_direction(self):
        from beat_align.detect import Beat

        beats = [Beat(t, 1.0) for t in (0, 1000, 2000, 3000)]
        _write_shots(self.shots, [
            {"title": "精确命中", "duration_ms": 1000},      # 起点 0
            {"title": "偏后", "duration_ms": 1000},          # 起点 1000
            {"title": "偏前", "duration_ms": 1000},          # 起点 2000
        ])
        entries = load_shots(self.shots)
        # 手工改造原始起点以覆盖三个方向
        entries[1].original_start_ms = 1200   # 最近鼓点 1000，向前 200
        entries[2].original_start_ms = 1850   # 最近鼓点 2000，向后 150
        aligned = align_shots(entries, beats)
        self.assertEqual(aligned[0].offset_ms, 0)
        self.assertEqual(aligned[0].direction, DIRECTION_NONE)
        self.assertEqual(aligned[1].offset_ms, abs(1000 - 1200))
        self.assertEqual(aligned[1].direction, DIRECTION_EARLIER)
        self.assertEqual(aligned[1].signed_offset_ms, -200)
        self.assertEqual(aligned[2].offset_ms, abs(2000 - 1850))
        self.assertEqual(aligned[2].direction, DIRECTION_LATER)
        self.assertEqual(aligned[2].signed_offset_ms, 150)

        # 等距时取更早鼓点（1500 距 1000 / 2000 等距）
        beat_time, gap = nearest_beat(1500, beats)
        self.assertEqual(beat_time, 1000)
        self.assertEqual(gap, 500)

    # --- 需求 5：三张预览图、文件名有序、含标题与时码 ----------------------
    def test_05_preview_images(self):
        _write_shots(self.shots, [
            {"title": "镜头甲", "duration_ms": 2200},
            {"title": "镜头乙", "duration_ms": 3300},
            {"title": "镜头丙", "duration_ms": 4500},
        ])
        info = load_wav(self.wav)
        beats = detect_beats(info)
        aligned = align_shots(load_shots(self.shots), beats)
        paths = render_all(aligned, beats, info.duration_ms, self.out)
        self.assertEqual(len(paths), 3)
        self.assertEqual(sorted(paths), paths)  # 编号零填充，字典序即镜头顺序
        for path in paths:
            self.assertTrue(os.path.exists(path))
            with Image.open(path) as img:
                self.assertEqual(img.size, (1280, 720))
                img.verify()
        self.assertTrue(os.path.basename(paths[0]).startswith("shot_001_"))

    def test_05b_timecode_format(self):
        self.assertEqual(format_timecode(0), "00:00:00.000")
        self.assertEqual(format_timecode(3_723_456), "01:02:03.456")

    # --- 需求 6：零时长 / 缺时长跳过，其余正常 -----------------------------
    def test_06_zero_or_missing_duration_skipped(self):
        _write_shots(self.shots, [
            {"title": "正常一", "duration_ms": 2000},
            {"title": "零时长", "duration_ms": 0},
            {"title": "缺时长"},
        ])
        entries = load_shots(self.shots)
        self.assertFalse(entries[0].skipped)
        self.assertTrue(entries[1].skipped)
        self.assertIn("0", entries[1].skip_reason)
        self.assertTrue(entries[2].skipped)
        self.assertIn("缺少", entries[2].skip_reason)

        info = load_wav(self.wav)
        result = run_pipeline(self.wav, self.shots, self.out, self.report)
        self.assertEqual(result.report["summary"]["completed_count"], 1)
        self.assertEqual(result.report["summary"]["incomplete_count"], 2)
        pngs = [f for f in os.listdir(self.out) if f.endswith(".png")]
        self.assertEqual(len(pngs), 1)

    # --- 需求 7：损坏 WAV 明确报错且零产出 ---------------------------------
    def test_07_corrupt_wav_no_outputs(self):
        corrupt = os.path.join(self.dir, "corrupt.wav")
        with open(corrupt, "wb") as handle:
            handle.write(b"RIFF????WAVEbroken-bytes-not-a-real-file")
        _write_shots(self.shots, [{"title": "A", "duration_ms": 1000}])
        before = set(os.listdir(self.dir))
        with self.assertRaises(AudioReadError) as ctx:
            run_pipeline(corrupt, self.shots, self.out, self.report)
        self.assertIn("读取失败", str(ctx.exception))
        after = set(os.listdir(self.dir))
        self.assertEqual(before, after)  # 无任何新增文件，预览与 JSON 都不存在
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.report))

        missing = os.path.join(self.dir, "nope.wav")
        with self.assertRaises(AudioReadError):
            load_wav(missing)

    # --- 需求 8：核查 JSON 顺序与逐字段可核算 ------------------------------
    def test_08_report_json_order_and_fields(self):
        _write_shots(self.shots, [
            {"title": "甲", "duration_ms": 1200},
            {"title": "乙", "duration_ms": 2600},
            {"title": "丙", "duration_ms": 800},
        ])
        result = run_pipeline(self.wav, self.shots, self.out, self.report)
        with open(self.report, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual([item["title"] for item in data["completed"]], ["甲", "乙", "丙"])
        expected_starts = [0, 1200, 3800]
        for item, original in zip(data["completed"], expected_starts):
            self.assertEqual(item["original_start_ms"], original)
            # 逐字段核算：偏移量 == |对齐后 - 原始|，对齐后 == 所用鼓点
            self.assertEqual(
                item["offset_ms"],
                abs(item["aligned_start_ms"] - item["original_start_ms"]),
            )
            self.assertEqual(item["aligned_start_ms"], item["beat_time_ms"])
            self.assertIn(item["direction"], ("earlier", "later", "none"))
        self.assertEqual(data["detector"]["beat_times_ms"],
                         [b.time_ms for b in result.beats])

    # --- 需求 9：目录不可写给提示，JSON 保持可读 ---------------------------
    def test_09_unwritable_dir_but_report_readable(self):
        _write_shots(self.shots, [
            {"title": "甲", "duration_ms": 1200},
            {"title": "乙", "duration_ms": 2600},
        ])
        # 用同名文件占用输出路径，跨平台稳定触发不可写
        blocked = os.path.join(self.dir, "blocked_path")
        with open(blocked, "wb") as handle:
            handle.write(b"x")
        result = run_pipeline(self.wav, self.shots, blocked, self.report)
        self.assertIsNotNone(result.preview_error)
        self.assertIn("不可写", result.preview_error)
        self.assertTrue(os.path.exists(self.report))
        with open(self.report, encoding="utf-8") as handle:
            data = json.load(handle)  # JSON 可正常打开
        self.assertEqual(data["summary"]["completed_count"], 2)

        # 直接调用预检也应抛出对应异常
        with self.assertRaises(OutputNotWritableError):
            from beat_align.preview import ensure_output_dir
            ensure_output_dir(blocked)

    # --- 需求 10：已完成 / 未完成分区，未完成不冒充成功 --------------------
    def test_10_completed_vs_incomplete_separation(self):
        _write_shots(self.shots, [
            {"title": "好镜头1", "duration_ms": 1500},
            {"title": "坏镜头", "duration_ms": 0},
            {"title": "好镜头2", "duration_ms": 3000},
        ])
        result = run_pipeline(self.wav, self.shots, self.out, self.report)
        report = result.report
        self.assertEqual([i["title"] for i in report["completed"]], ["好镜头1", "好镜头2"])
        self.assertEqual([i["title"] for i in report["incomplete"]], ["坏镜头"])
        bad = report["incomplete"][0]
        self.assertEqual(bad["status"], "skipped")
        self.assertTrue(bad["reason"])
        self.assertNotIn("aligned_start_ms", bad)  # 未完成条目不得带对齐字段
        for good in report["completed"]:
            self.assertEqual(good["status"], "aligned")
            for key in ("aligned_start_ms", "original_start_ms", "offset_ms", "beat_time_ms"):
                self.assertIn(key, good)
        # 落盘后的 JSON 保持同样分区
        with open(self.report, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["summary"]["incomplete_count"], 1)

    def test_10b_no_beats_all_incomplete(self):
        # 静音配乐检不出鼓点：全部镜头如实进入未完成区域
        silent = os.path.join(self.dir, "silent.wav")
        _write_wav(silent, [0] * SR * 4)
        _write_shots(self.shots, [{"title": "A", "duration_ms": 1000}])
        result = run_pipeline(silent, self.shots, self.out, self.report)
        self.assertEqual(len(result.beats), 0)
        self.assertEqual(result.report["summary"]["completed_count"], 0)
        self.assertEqual(result.report["summary"]["incomplete_count"], 1)
        self.assertIn("未检出", result.report["incomplete"][0]["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
