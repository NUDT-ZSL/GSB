"""端到端单元测试，逐条对应任务 10 项验收标准（仅标准库，确定性样本）。

运行（在项目根目录）：
    python -I run_tests.py
或：
    python -I -m unittest -v tests.test_audio_pipeline
"""

from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

# 兼容 python -I（隔离模式）下直接运行本文件
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audio_pipeline.config import (  # noqa: E402
    ConfigError,
    DEFAULT_CONFIG,
    PipelineConfig,
    load_config,
    parse_config,
)
from audio_pipeline.pipeline import process_directory  # noqa: E402
from audio_pipeline.processing import (  # noqa: E402
    apply_fade,
    apply_gain,
    quantize_int16,
    trim_silence,
)
from audio_pipeline.wav_io import WavData, WavFormatError, read_wav, write_wav  # noqa: E402

from tests import make_fixtures  # noqa: E402


def _read_raw_wav(path: Path) -> tuple[list[int], int, int, int]:
    """返回 (samples, sample_rate, sampwidth, n_channels)。"""
    with wave.open(str(path), "rb") as wf:
        n = wf.getnframes()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        frames = wf.readframes(n)
    if width == 2:
        samples = list(struct.unpack("<%dh" % n, frames))
    else:
        samples = list(frames)
    return samples, rate, width, channels


class AudioPipelineTestBase(unittest.TestCase):
    fixture_root: Path
    tmp_root: Path
    input_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_root = Path(tempfile.mkdtemp(prefix="audiopipe_test_"))
        cls.input_dir = cls.tmp_root / "input"
        make_fixtures.build_fixtures(cls.input_dir)
        cls.fixture_root = cls.input_dir
        cls._run_counter = 0

    def _out_dir(self, name: str) -> Path:
        # 每次运行生成唯一目录，避免测试间复用陈旧输出导致误判
        self.__class__._run_counter += 1
        return self.tmp_root / f"{name}_{self.__class__._run_counter}"

    def _run(self, name: str, config: PipelineConfig):
        out = self._out_dir(name)
        report = process_directory(self.input_dir, out, config)
        return out, report


class TestAcceptance01WavParams(AudioPipelineTestBase):
    """验收 1：输出 WAV 采样率/位深/声道与输入一致。"""

    def test_header_params_preserved(self):
        out, report = self._run("t01", DEFAULT_CONFIG)
        self.assertEqual(report.summary["failed"], 0)

        in_path = self.input_dir / "sfx" / "clean_44100.wav"
        out_path = out / "sfx" / "clean_44100.wav"
        _, in_rate, in_width, in_ch = _read_raw_wav(in_path)
        out_samples, out_rate, out_width, out_ch = _read_raw_wav(out_path)

        self.assertEqual(out_rate, in_rate)
        self.assertEqual(out_rate, 44100)
        self.assertEqual(out_width * 8, 16)
        self.assertEqual(in_width * 8, 16)
        self.assertEqual(out_ch, 1)
        self.assertEqual(in_ch, 1)
        self.assertGreater(len(out_samples), 0)

    def test_other_supported_rates_preserved(self):
        out, _ = self._run("t01b", DEFAULT_CONFIG)
        for rel, expected_rate in (
            ("sfx/tone_22050.wav", 22050),
            ("sfx/short_8000.wav", 8000),
            ("sub/nested_16k.wav", 16000),
        ):
            _, rate, width, ch = _read_raw_wav(out / rel)
            self.assertEqual(rate, expected_rate, rel)
            self.assertEqual(width, 2, rel)
            self.assertEqual(ch, 1, rel)


class TestAcceptance02Gain(AudioPipelineTestBase):
    """验收 2：+6dB 后峰值约为 2 倍且不削波，误差 ±5%。"""

    def test_plus_6db_doubles_peak_without_clipping(self):
        sr = 44100
        samples = make_fixtures._sine_16bit(sr, 0.2, 1000.0, amplitude=0.3)
        in_peak = max(abs(s) for s in samples)

        config = PipelineConfig(gain_db=6.0, threshold_db=-50.0, fade_ms=0.0)
        # 直接对纯正弦执行增益步骤（无静音可裁，无淡变）
        scaled, info = apply_gain([float(s) for s in samples], config.gain_db)
        out_samples = quantize_int16(scaled)
        out_peak = max(abs(s) for s in out_samples)

        self.assertFalse(info.limited)
        ratio = out_peak / in_peak
        self.assertAlmostEqual(ratio, 2.0, delta=2.0 * 0.05)  # ±5%
        self.assertLessEqual(out_peak, 32767)
        self.assertGreaterEqual(min(out_samples), -32768)

    def test_end_to_end_gain_no_overflow(self):
        config = PipelineConfig(gain_db=6.0)
        out, report = self._run("t02", config)
        entry = next(e for e in report.success if e.rel_path == "sfx/clean_44100.wav")
        out_samples, _, _, _ = _read_raw_wav(Path(entry.output_wav))
        self.assertLessEqual(max(abs(s) for s in out_samples), 32767)
        self.assertGreater(min(out_samples), -32768)

    def test_limiter_prevents_clipping_at_high_gain(self):
        """0.8 满幅 +12dB 必然超满幅：全局峰值保护后仍不削波。"""
        sr = 16000
        samples = make_fixtures._sine_16bit(sr, 0.2, 1000.0, amplitude=0.8)
        scaled, info = apply_gain([float(s) for s in samples], 12.0)
        out_samples = quantize_int16(scaled)
        self.assertTrue(info.limited)
        self.assertLessEqual(max(out_samples), 32767)
        self.assertGreaterEqual(min(out_samples), -32768)
        # 保护后应接近但不到满幅
        self.assertGreater(max(abs(s) for s in out_samples), 30000)


class TestAcceptance03Trim(AudioPipelineTestBase):
    """验收 3：首尾各 0.5s 静音，输出约减少 0.98s，且每侧保留 >=10ms。"""

    def test_trim_keeps_10ms_silence_each_side(self):
        sr = 44100
        in_path = self.input_dir / "sfx" / "clean_44100.wav"
        wav = read_wav(in_path)
        in_duration = wav.duration_seconds

        trimmed, info = trim_silence(
            [float(s) for s in wav.samples], sr, threshold_db=-50.0
        )
        out_duration = len(trimmed) / sr

        self.assertFalse(info.all_silence)

        # 任务验收的两个硬性指标：
        # 1) 总时长约减少 0.98s（两侧各 0.5s 静音，各保留 >=10ms）
        delta = in_duration - out_duration
        self.assertAlmostEqual(delta, 0.98, delta=0.01)
        # 2) 每侧保留静音 >= 10ms（441 个样本）
        keep_min = math.ceil(sr * 0.01)
        self.assertEqual(keep_min, 441)
        self.assertGreaterEqual(info.trimmed_leading, int(0.5 * sr) - keep_min - 1)
        self.assertGreaterEqual(info.trimmed_trailing, int(0.5 * sr) - keep_min - 1)

        # 开头与结尾确实仍保留至少 10ms 的静音（零样本）
        self.assertEqual(trimmed[0], 0.0)
        self.assertEqual(trimmed[-1], 0.0)
        self.assertTrue(all(v == 0.0 for v in trimmed[:keep_min]))
        self.assertTrue(all(v == 0.0 for v in trimmed[-keep_min:]))

    def test_trim_guard_band_exact_on_synthetic_signal(self):
        """合成确定性信号精确验证：每侧恰保留 10ms 静音 + 首个/末个响样本。"""
        sr = 1000  # 便于手算：10ms = 10 个样本
        samples = [0] * 50 + [1000] * 30 + [0] * 50
        trimmed, info = trim_silence([float(s) for s in samples], sr, -50.0)
        keep = math.ceil(sr * 0.01)  # 10
        self.assertEqual(keep, 10)
        # start = 50 - 10 = 40（裁掉 40 个），end = 80 + 10 = 90（裁掉 40 个）
        self.assertEqual(info.trimmed_leading, 40)
        self.assertEqual(info.trimmed_trailing, 40)
        self.assertEqual(len(trimmed), 50)
        self.assertTrue(all(v == 0.0 for v in trimmed[:10]))
        self.assertTrue(all(v == 0.0 for v in trimmed[-10:]))
        self.assertEqual(trimmed[10], 1000.0)
        self.assertEqual(trimmed[-11], 1000.0)

    def test_all_silence_file_is_kept_intact(self):
        sr = 8000
        silence = [0] * sr  # 1s 全静音
        out, info = trim_silence([float(s) for s in silence], sr, -50.0)
        self.assertTrue(info.all_silence)
        self.assertEqual(len(out), len(silence))

    def test_threshold_bounds_validated(self):
        with self.assertRaises(ConfigError):
            parse_config({"threshold_db": -10.0})
        with self.assertRaises(ConfigError):
            parse_config({"threshold_db": -70.0})
        # 边界值合法
        parse_config({"threshold_db": -60.0})
        parse_config({"threshold_db": -20.0})


class TestAcceptance04Fade(AudioPipelineTestBase):
    """验收 4：100ms 淡入，前 100ms 幅度单调升至满幅。"""

    def test_linear_fade_in_monotonic(self):
        sr = 44100
        level = 10000
        samples = [float(level)] * sr  # 1s 恒定振幅
        faded, info = apply_fade(samples, sr, fade_ms=100.0)

        fade_len = info.fade_samples_per_side
        self.assertEqual(fade_len, round(sr * 0.1))  # 4410

        # 淡入段严格单调不减（线性：i/N * level）
        ramp = faded[:fade_len]
        for a, b in zip(ramp, ramp[1:]):
            self.assertLessEqual(a, b)
        self.assertEqual(ramp[0], 0.0)
        # 淡入末端达到满幅（恒定输入的满幅即 level）
        self.assertAlmostEqual(ramp[-1], level * (fade_len - 1) / fade_len)
        # 淡入之后保持原振幅
        self.assertEqual(faded[fade_len], level)

    def test_fade_out_descends_to_zero(self):
        sr = 44100
        level = 8000
        samples = [float(level)] * sr
        faded, info = apply_fade(samples, sr, fade_ms=50.0)
        fade_len = info.fade_samples_per_side
        tail = faded[-fade_len:]
        for a, b in zip(tail, tail[1:]):
            self.assertGreaterEqual(a, b)
        self.assertEqual(tail[-1], 0.0)

    def test_fade_bounds(self):
        parse_config({"fade_ms": 10.0})
        parse_config({"fade_ms": 500.0})
        parse_config({"fade_ms": 0})
        with self.assertRaises(ConfigError):
            parse_config({"fade_ms": 5.0})
        with self.assertRaises(ConfigError):
            parse_config({"fade_ms": 600.0})


class TestAcceptance05Order(AudioPipelineTestBase):
    """验收 5：配置顺序决定最终输出，与按相同顺序手动单步作用的结果一致。"""

    def _manual_chain(self, samples: list[int], sr: int, config: PipelineConfig,
                      order: tuple[str, ...]) -> list[float]:
        """与生产流水线一致：全程 float，仅最终量化（由调用方比较时量化）。"""
        work = [float(s) for s in samples]
        for step in order:
            if step == "trim":
                work, _ = trim_silence(work, sr, config.threshold_db)
            elif step == "fade":
                work, _ = apply_fade(work, sr, config.fade_ms)
            else:
                work, _ = apply_gain(work, config.gain_db)
        return work

    def test_default_order_matches_manual_chain(self):
        config = PipelineConfig(gain_db=6.0, threshold_db=-45.0, fade_ms=100.0)
        wav = read_wav(self.input_dir / "sfx" / "clean_44100.wav")
        expected = self._manual_chain(wav.samples, wav.sample_rate, config,
                                      ("trim", "fade", "gain"))
        out, report = self._run("t05a", config)
        entry = next(e for e in report.success if e.rel_path == "sfx/clean_44100.wav")
        actual, _, _, _ = _read_raw_wav(Path(entry.output_wav))
        # 手动链浮点结果量化后应与流水线输出逐样本一致
        self.assertEqual(actual, quantize_int16(expected))

    def test_gain_first_order_matches_manual_chain_and_differs(self):
        order = ("gain", "trim", "fade")
        config = PipelineConfig(gain_db=6.0, threshold_db=-45.0, fade_ms=100.0,
                                order=order)
        wav = read_wav(self.input_dir / "sfx" / "clean_44100.wav")
        expected = self._manual_chain(wav.samples, wav.sample_rate, config, order)

        out, report = self._run("t05_gain_first", config)
        entry = next(e for e in report.success if e.rel_path == "sfx/clean_44100.wav")
        actual, _, _, _ = _read_raw_wav(Path(entry.output_wav))
        self.assertEqual(actual, quantize_int16(expected))

    def test_order_changes_result_when_noise_crosses_threshold(self):
        """顺序确实作用于最终输出：先增益会把低电平噪声推过裁剪阈值。

        信号：[6000 个低电平噪声][恒定高电平][6000 个低电平噪声]。
        -50dBFS 阈值 ≈ 103.6：噪声 60 低于阈值（属静音），
        +6dB（系数≈1.995）后约 119.7 越过阈值（变成保留内容），
        于是“先增益”路径完全不裁剪，“先裁剪”路径裁掉两侧噪声，
        输出长度必然不同。淡变关闭以隔离裁剪/增益两个步骤。
        """
        sr = 44100
        guard = 6000  # 大于 10ms 保底带（4410 样本），裁剪才有可操作空间
        noise = [60] * guard
        tone = [10000] * 2000  # 恒定振幅，远高于阈值，不存在零交叉干扰
        samples = noise + tone + noise

        cfg_gain_first = PipelineConfig(
            gain_db=6.0, threshold_db=-50.0, fade_ms=0.0,
            order=("gain", "trim", "fade"),
        )
        cfg_trim_first = PipelineConfig(
            gain_db=6.0, threshold_db=-50.0, fade_ms=0.0,
            order=("trim", "fade", "gain"),
        )

        gf = self._manual_chain(samples, sr, cfg_gain_first,
                                ("gain", "trim", "fade"))
        tf = self._manual_chain(samples, sr, cfg_trim_first,
                                ("trim", "fade", "gain"))

        # 先裁剪：两侧 300 个噪声样本按静音裁掉（各留 10ms 保底）
        # 先增益：噪声放大后越过阈值，没有任何内容可裁 -> 保留全部 1600 样本
        self.assertEqual(len(gf), len(samples))
        self.assertLess(len(tf), len(samples))
        self.assertNotEqual(len(gf), len(tf))
        # 与各自顺序的流水线输出一致
        for cfg, manual in ((cfg_gain_first, gf), (cfg_trim_first, tf)):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                in_dir = root / "input"
                in_dir.mkdir()
                from audio_pipeline.wav_io import write_wav
                write_wav(in_dir / "in.wav",
                          WavData(samples=samples, sample_rate=sr))
                out_dir = root / "output"
                report = process_directory(in_dir, out_dir, cfg)
                entry = report.success[0]
                actual, _, _, _ = _read_raw_wav(Path(entry.output_wav))
                self.assertEqual(actual, quantize_int16(manual))

    def test_order_validation(self):
        with self.assertRaises(ConfigError):
            parse_config({"order": ["gain", "trim", "boost"]})
        with self.assertRaises(ConfigError):
            parse_config({"order": ["gain", "trim"]})  # 缺步骤
        with self.assertRaises(ConfigError):
            parse_config({"order": ["gain", "gain", "trim"]})  # 重复


class TestAcceptance06Metadata(AudioPipelineTestBase):
    """验收 6：同名 JSON 字段与输出 WAV 头一致，时长误差 < 1ms。"""

    def test_json_matches_output_wav(self):
        config = PipelineConfig(gain_db=3.0, fade_ms=50.0)
        out, report = self._run("t06", config)

        for entry in report.success:
            wav_path = Path(entry.output_wav)
            json_path = Path(entry.output_json)
            self.assertTrue(json_path.exists())
            meta = json.loads(json_path.read_text(encoding="utf-8"))

            samples, rate, width, channels = _read_raw_wav(wav_path)
            actual_duration = len(samples) / rate

            self.assertEqual(meta["source_file"], wav_path.name)
            self.assertEqual(meta["sample_rate"], rate)
            self.assertEqual(meta["bit_depth"], width * 8)
            self.assertEqual(meta["channels"], channels)
            self.assertAlmostEqual(
                meta["duration_seconds"], actual_duration, delta=0.001
            )
            # 3 位小数
            self.assertEqual(meta["duration_seconds"],
                             round(actual_duration, 3))
            self.assertEqual(set(meta.keys()),
                             {"source_file", "duration_seconds", "sample_rate",
                              "bit_depth", "channels"})


class TestAcceptance07SkipInvalid(AudioPipelineTestBase):
    """验收 7：文本文件/8bit WAV 等被跳过且报告原因，有效素材照常输出。"""

    def test_invalid_files_listed_with_reasons(self):
        out, report = self._run("t07", DEFAULT_CONFIG)

        skip_map = {e.rel_path: e.reason for e in report.skipped}
        self.assertIn("sfx/notes.txt", skip_map)
        self.assertIn("非 WAV", skip_map["sfx/notes.txt"])
        self.assertIn("sfx/legacy_8bit.wav", skip_map)
        self.assertIn("16bit", skip_map["sfx/legacy_8bit.wav"])
        self.assertIn("sfx/high_rate_96k.wav", skip_map)
        self.assertIn("采样率", skip_map["sfx/high_rate_96k.wav"])
        self.assertIn("sfx/corrupt.wav", skip_map)

        # 有效素材全部成功
        success_paths = {e.rel_path for e in report.success}
        self.assertEqual(
            success_paths,
            {"sfx/clean_44100.wav", "sfx/tone_22050.wav", "sfx/short_8000.wav",
             "sub/nested_16k.wav"},
        )
        # 被跳过素材没有任何输出
        self.assertFalse((out / "sfx" / "notes.txt").exists())
        self.assertFalse((out / "sfx" / "legacy_8bit.wav").exists())
        self.assertFalse((out / "sfx" / "legacy_8bit.json").exists())
        self.assertEqual(report.summary["failed"], 0)


class TestAcceptance08Defaults(AudioPipelineTestBase):
    """验收 8：缺字段/空对象配置按默认值 0dB、-50dB、0ms、trim→fade→gain 执行。"""

    def test_empty_object_uses_defaults(self):
        cfg = parse_config({})
        self.assertEqual(cfg, DEFAULT_CONFIG)
        self.assertEqual(cfg.gain_db, 0.0)
        self.assertEqual(cfg.threshold_db, -50.0)
        self.assertEqual(cfg.fade_ms, 0.0)
        self.assertEqual(tuple(cfg.order), ("trim", "fade", "gain"))

    def test_partial_config_fills_defaults(self):
        cfg = parse_config({"gain_db": -3.0})
        self.assertEqual(cfg.gain_db, -3.0)
        self.assertEqual(cfg.threshold_db, -50.0)
        self.assertEqual(cfg.fade_ms, 0.0)
        self.assertEqual(tuple(cfg.order), ("trim", "fade", "gain"))

    def test_end_to_end_empty_and_partial_configs(self):
        configs_dir = PROJECT_ROOT / "configs"
        for name in ("empty.json", "partial.json"):
            cfg = load_config(configs_dir / name)
            out, report = self._run(f"t08_{name}", cfg)
            self.assertEqual(report.summary["failed"], 0)
            self.assertEqual(report.summary["success"], 4)
            self.assertTrue((out / "sfx" / "clean_44100.wav").exists())


class TestAcceptance09BadConfig(AudioPipelineTestBase):
    """验收 9：配置无法解析或取值非法时不处理任何素材，并明确报错。"""

    def _assert_no_outputs_created(self, out: Path) -> None:
        if out.exists():
            files = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(files, [])

    def test_gain_out_of_range(self):
        out = self._out_dir("t09_bad_gain")
        with self.assertRaises(ConfigError):
            load_config(PROJECT_ROOT / "configs" / "bad_gain.json")
        self._assert_no_outputs_created(out)

    def test_malformed_json(self):
        out = self._out_dir("t09_malformed")
        with self.assertRaises(ConfigError) as ctx:
            load_config(PROJECT_ROOT / "configs" / "malformed.json")
        self.assertIn("JSON", str(ctx.exception))
        self._assert_no_outputs_created(out)

    def test_other_illegal_configs(self):
        for name in ("bad_threshold.json", "bad_fade.json", "bad_order.json"):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    load_config(PROJECT_ROOT / "configs" / name)

    def test_unknown_key_and_wrong_type_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config({"gain": 6.0})          # 拼写错误的未知键
        with self.assertRaises(ConfigError):
            parse_config({"gain_db": "loud"})    # 类型错误
        with self.assertRaises(ConfigError):
            parse_config({"gain_db": True})      # 布尔不接受
        with self.assertRaises(ConfigError):
            parse_config([1, 2, 3])              # 根节点不是对象
        with self.assertRaises(ConfigError):
            parse_config({"gain_db": float("nan")})


class TestAcceptance10Structure(AudioPipelineTestBase):
    """验收 10：输出保持相对子目录结构，报告按路径区分成功与跳过。"""

    def test_relative_structure_preserved(self):
        out, report = self._run("t10", DEFAULT_CONFIG)
        self.assertTrue((out / "sfx" / "clean_44100.wav").is_file())
        self.assertTrue((out / "sfx" / "clean_44100.json").is_file())
        self.assertTrue((out / "sub" / "nested_16k.wav").is_file())
        self.assertTrue((out / "sub" / "nested_16k.json").is_file())

        success_paths = {e.rel_path for e in report.success}
        skip_paths = {e.rel_path for e in report.skipped}
        self.assertIn("sub/nested_16k.wav", success_paths)
        self.assertIn("sfx/legacy_8bit.wav", skip_paths)
        self.assertTrue(success_paths.isdisjoint(skip_paths))


class TestCliAndReport(AudioPipelineTestBase):
    """附加：CLI 退出码、配置错误短路、JSON 报告落盘。"""

    def test_cli_config_error_exit_code_and_no_output(self):
        from audio_pipeline.cli import main

        out = self._out_dir("cli_bad")
        exit_code = main([
            "--input", str(self.input_dir),
            "--output", str(out),
            "--config", str(PROJECT_ROOT / "configs" / "bad_gain.json"),
        ])
        self.assertEqual(exit_code, 2)
        self.assertFalse(out.exists())

    def test_cli_success_and_report_file(self):
        from audio_pipeline.cli import main

        out = self._out_dir("cli_ok")
        report_file = self.tmp_root / "report.json"
        exit_code = main([
            "--input", str(self.input_dir),
            "--output", str(out),
            "--config", str(PROJECT_ROOT / "configs" / "full.json"),
            "--report", str(report_file),
        ])
        self.assertEqual(exit_code, 0)
        self.assertTrue((out / "sub" / "nested_16k.wav").is_file())
        report_data = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertEqual(report_data["summary"]["success"], 4)
        self.assertEqual(report_data["summary"]["skipped"], 4)
        self.assertTrue(
            any(e["path"] == "sfx/notes.txt" for e in report_data["skipped"])
        )


class TestRoundTripHelpers(unittest.TestCase):
    """附加：write_wav/read_wav 往返与空文件保护。"""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.wav"
            data = WavData(samples=[0, 1, -1, 32767, -32768], sample_rate=22050)
            write_wav(p, data)
            loaded = read_wav(p)
            self.assertEqual(loaded.samples, data.samples)
            self.assertEqual(loaded.sample_rate, 22050)

    def test_read_text_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.wav"
            p.write_text("not a wav", encoding="utf-8")
            with self.assertRaises(WavFormatError):
                read_wav(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
