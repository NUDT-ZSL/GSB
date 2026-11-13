#!/usr/bin/env python3
"""compose_audio.py 的验收测试。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import array
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "compose_audio.py"
SAMPLE_RATE = 8000
INT16_MAX = 32767


# --------------------------------------------------------------------------
# 样本生成
# --------------------------------------------------------------------------
def sine(duration_s: float, frequency: float, amplitude: float) -> array.array:
    frames = int(round(duration_s * SAMPLE_RATE))
    out = array.array("h")
    for i in range(frames):
        out.append(int(round(amplitude * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE))))
    return out


def write_wav(
    path: Path,
    samples: array.array,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = SAMPLE_RATE,
) -> None:
    if channels == 2:
        doubled = array.array("h")
        for value in samples:
            doubled.append(value)
            doubled.append(value)
        samples = doubled
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def write_tone(path: Path, duration_s: float, frequency: float, amplitude: float = 8000.0) -> None:
    write_wav(path, sine(duration_s, frequency, amplitude))


def read_wav(path: Path) -> tuple[array.array, int, int, int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, channels, width, rate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ComposeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def make_manifest(self, items: list[dict], name: str = "manifest.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, manifest: Path, out: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(manifest), str(out or self.out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )

    def report(self) -> dict:
        return json.loads((self.out / "report.json").read_text(encoding="utf-8"))

    # -- 1. 清单格式校验 ----------------------------------------------------
    def test_1_negative_start_rejected(self) -> None:
        write_tone(self.tmp / "a.wav", 0.5, 440.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": -1.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("清单格式错误", result.stderr)
        self.assertFalse((self.out / "output.wav").exists())

    def test_1_missing_path_rejected(self) -> None:
        manifest = self.make_manifest(
            [{"start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("清单格式错误", result.stderr)

    def test_1_non_array_manifest_rejected(self) -> None:
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps({"path": "a.wav"}), encoding="utf-8")
        result = self.run_cli(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("清单格式错误", result.stderr)

    def test_1_bool_is_not_a_number(self) -> None:
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": True, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("清单格式错误", result.stderr)

    def test_1_oversized_start_rejected(self) -> None:
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 1e12, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("清单格式错误", result.stderr)

    # -- 2. 缺失片段 --------------------------------------------------------
    def test_2_missing_clip_skipped(self) -> None:
        write_tone(self.tmp / "a.wav", 1.0, 440.0)
        manifest = self.make_manifest(
            [
                {"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
                {"path": "nope.wav", "start": 0.5, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report["片段"][1]["状态"], "缺失")
        self.assertEqual(report["片段"][1]["错误信息"], "文件不存在")
        self.assertFalse(report["完整"])
        samples, _, _, _ = read_wav(self.out / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE)  # 1 秒
        self.assertGreater(max(abs(s) for s in samples), 1000)
        # 其余片段应原样进入输出。
        source, _, _, _ = read_wav(self.tmp / "a.wav")
        self.assertEqual(samples, source)

    # -- 3. 格式不支持 -------------------------------------------------------
    def test_3_stereo_clip_unsupported(self) -> None:
        write_tone(self.tmp / "mono.wav", 1.0, 440.0)
        write_wav(
            self.tmp / "stereo.wav",
            sine(1.0, 550.0, 5000.0),
            channels=2,
        )
        manifest = self.make_manifest(
            [
                {"path": "mono.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
                {"path": "stereo.wav", "start": 1.0, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report["片段"][1]["状态"], "格式不支持")
        self.assertFalse(report["完整"])
        samples, channels, width, rate = read_wav(self.out / "output.wav")
        self.assertEqual((channels, width, rate), (1, 2, SAMPLE_RATE))
        # 只有第一个片段被合成，时长仍为 1 秒，且音频与源片段一致。
        self.assertEqual(len(samples), SAMPLE_RATE)
        source, _, _, _ = read_wav(self.tmp / "mono.wav")
        self.assertEqual(samples, source)

    # -- 4. 相同 start 混合 ---------------------------------------------------
    def test_4_same_start_mix_no_clipping(self) -> None:
        # 两个接近满幅的正弦叠加，峰值远超 int16；应线性缩放而硬削波。
        a = sine(1.0, 440.0, 32000.0)
        b = sine(1.0, 660.0, 32000.0)
        write_wav(self.tmp / "a.wav", a)
        write_wav(self.tmp / "b.wav", b)
        manifest = self.make_manifest(
            [
                {"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
                {"path": "b.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        samples, _, _, _ = read_wav(self.out / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE)

        mixed = [a[i] + b[i] for i in range(len(samples))]
        peak = max(abs(value) for value in mixed)
        self.assertGreater(peak, INT16_MAX)  # 确认确实过载过
        expected = [round(value * INT16_MAX / peak) for value in mixed]
        max_error = max(abs(s - e) for s, e in zip(samples, expected))
        # 输出应等于线性缩放后的混音，而不是被截断的波形。
        self.assertLessEqual(max_error, 1)

        # 硬削波会产生连续顶到边界的长平台；线性缩放后最多只有孤立峰值点。
        longest_run = run = 0
        for sample in samples:
            if abs(sample) >= INT16_MAX:
                run += 1
                longest_run = max(longest_run, run)
            else:
                run = 0
        self.assertLessEqual(longest_run, 2, f"检测到削波平台，长度 {longest_run}")

    # -- 5. 淡入淡出 ---------------------------------------------------------
    def test_5_fade_in_longer_than_clip(self) -> None:
        # 频率取非整数赫兹，避免在 1/2 秒等采样点恰好过零导致比值无意义。
        write_tone(self.tmp / "a.wav", 1.0, 300.7, 8000.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 2.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        faded, _, _, _ = read_wav(self.out / "output.wav")

        # 与无淡入版本逐点比较，增益应从 ~0 线性增长到 1。
        no_fade_out = self.tmp / "nofade"
        manifest2 = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}],
            name="manifest_nofade.json",
        )
        result = self.run_cli(manifest2, no_fade_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        plain, _, _, _ = read_wav(no_fade_out / "output.wav")

        def gain_at(index: int) -> float:
            if plain[index] == 0:
                return float("nan")
            return faded[index] / plain[index]

        self.assertLess(gain_at(10), 0.01)
        self.assertAlmostEqual(gain_at(SAMPLE_RATE // 2), 0.5, delta=0.02)
        self.assertGreater(gain_at(SAMPLE_RATE - 20), 0.97)

    def test_5_fade_out_ends_at_clip_end(self) -> None:
        write_tone(self.tmp / "a.wav", 1.0, 440.0, 8000.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 1.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        samples, _, _, _ = read_wav(self.out / "output.wav")
        self.assertLess(abs(samples[-1]), 40)  # 最后一点接近静音

    # -- 6. 输出长度 ---------------------------------------------------------
    def test_6_total_duration_covers_timeline(self) -> None:
        write_tone(self.tmp / "a.wav", 3.0, 220.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 2.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        samples, _, _, _ = read_wav(self.out / "output.wav")
        self.assertGreaterEqual(len(samples), 5 * SAMPLE_RATE)
        self.assertEqual(len(samples), 5 * SAMPLE_RATE)
        self.assertEqual(self.report()["总时长"], 5.0)
        self.assertEqual(samples[0], 0)
        self.assertEqual(samples[2 * SAMPLE_RATE - 1], 0)

    def test_6_no_valid_clips_zero_sampled_wav(self) -> None:
        manifest = self.make_manifest(
            [{"path": "missing.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        wav_path = self.out / "output.wav"
        self.assertEqual(wav_path.stat().st_size, 44)
        with wave.open(str(wav_path), "rb") as wav:
            self.assertEqual(wav.getnframes(), 0)
        self.assertEqual(self.report()["总时长"], 0)
        self.assertFalse(self.report()["完整"])

    # -- 7. 报告结构 ---------------------------------------------------------
    def test_7_report_structure(self) -> None:
        write_tone(self.tmp / "a.wav", 1.0, 440.0)
        write_tone(self.tmp / "b.wav", 1.0, 660.0)
        manifest = self.make_manifest(
            [
                {"path": "a.wav", "start": 0.0, "fade_in": 0.1, "fade_out": 0.1},
                {"path": "b.wav", "start": 1.0, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(set(report), {"完整", "总时长", "片段"})
        self.assertIs(report["完整"], True)
        self.assertEqual(report["总时长"], 2.0)
        self.assertEqual([c["path"] for c in report["片段"]], ["a.wav", "b.wav"])
        for clip in report["片段"]:
            self.assertEqual(set(clip), {"path", "状态", "错误信息"})
            self.assertEqual(clip["状态"], "完整")
            self.assertIsNone(clip["错误信息"])

    # -- 8. 读取失败 ---------------------------------------------------------
    def test_8_corrupt_wav_read_error(self) -> None:
        write_tone(self.tmp / "good.wav", 1.0, 440.0)
        corrupt = self.tmp / "corrupt.wav"
        corrupt.write_bytes(b"NOTRIFF" + b"\x00" * 64)
        manifest = self.make_manifest(
            [
                {"path": "corrupt.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
                {"path": "good.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.report()
        self.assertEqual(report["片段"][0]["状态"], "读取失败")
        self.assertIsNotNone(report["片段"][0]["错误信息"])
        self.assertFalse(report["完整"])
        samples, _, _, _ = read_wav(self.out / "output.wav")
        self.assertEqual(len(samples), SAMPLE_RATE)
        self.assertGreater(max(abs(s) for s in samples), 1000)

    def test_8_truncated_wav_read_error(self) -> None:
        # 头部完好，但声明的 datachunk 长度大于实际文件内容。
        truncated = self.tmp / "truncated.wav"
        write_wav(truncated, sine(1.0, 440.0, 8000.0))
        data = truncated.read_bytes()
        truncated.write_bytes(data[:400])
        manifest = self.make_manifest(
            [{"path": "truncated.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["片段"][0]["状态"], "读取失败")

    def test_8_directory_instead_of_file(self) -> None:
        (self.tmp / "folder.wav").mkdir()
        manifest = self.make_manifest(
            [{"path": "folder.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report()["片段"][0]["状态"], "读取失败")
        self.assertEqual((self.out / "output.wav").stat().st_size, 44)

    # -- 9. 确定性 -----------------------------------------------------------
    def test_9_deterministic_outputs(self) -> None:
        write_tone(self.tmp / "a.wav", 1.0, 440.0)
        manifest = self.make_manifest(
            [
                {"path": "a.wav", "start": 0.2, "fade_in": 0.1, "fade_out": 0.1},
                {"path": "gone.wav", "start": 1.0, "fade_in": 0.0, "fade_out": 0.0},
            ]
        )
        out_a = self.tmp / "out_a"
        out_b = self.tmp / "out_b"
        self.assertEqual(self.run_cli(manifest, out_a).returncode, 0)
        self.assertEqual(self.run_cli(manifest, out_b).returncode, 0)
        for name in ("output.wav", "report.json"):
            self.assertEqual(
                sha256(out_a / name), sha256(out_b / name), f"{name} 不一致"
            )

    # -- 10. 命令行参数 ------------------------------------------------------
    def test_10_creates_both_outputs(self) -> None:
        write_tone(self.tmp / "a.wav", 0.5, 440.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        nested_out = self.tmp / "deep" / "out"
        result = self.run_cli(manifest, nested_out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((nested_out / "output.wav").is_file())
        self.assertTrue((nested_out / "report.json").is_file())

    # -- 11. 清单不可读 ------------------------------------------------------
    def test_11_missing_manifest_file(self) -> None:
        result = self.run_cli(self.tmp / "does_not_exist.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法读取清单", result.stderr)

    def test_11_invalid_json(self) -> None:
        manifest = self.tmp / "bad.json"
        manifest.write_text("{not json", encoding="utf-8")
        result = self.run_cli(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法读取清单", result.stderr)

    # -- 12. 错误信息字段 ----------------------------------------------------
    def test_12_error_message_null_when_valid(self) -> None:
        write_tone(self.tmp / "a.wav", 0.5, 440.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        self.assertEqual(self.run_cli(manifest).returncode, 0)
        clip = self.report()["片段"][0]
        self.assertIsNone(clip["错误信息"])

    # -- 13. WAV 头 ----------------------------------------------------------
    def test_13_wav_header_and_params(self) -> None:
        write_tone(self.tmp / "a.wav", 0.5, 440.0)
        manifest = self.make_manifest(
            [{"path": "a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        self.assertEqual(self.run_cli(manifest).returncode, 0)
        wav_path = self.out / "output.wav"
        header = wav_path.read_bytes()[:12]
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(header[8:12], b"WAVE")
        riff_size = struct.unpack("<I", header[4:8])[0]
        self.assertEqual(riff_size, wav_path.stat().st_size - 8)
        with wave.open(str(wav_path), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), SAMPLE_RATE)
            self.assertEqual(wav.getcomptype(), "NONE")

    # -- 14. 空清单 ----------------------------------------------------------
    def test_14_empty_manifest(self) -> None:
        manifest = self.make_manifest([])
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / "output.wav").stat().st_size, 44)
        report = self.report()
        self.assertIs(report["完整"], True)
        self.assertEqual(report["总时长"], 0)
        self.assertEqual(report["片段"], [])

    # -- 补充：片段路径相对清单目录解析 --------------------------------------
    def test_relative_paths_resolved_from_manifest_dir(self) -> None:
        assets = self.tmp / "assets"
        assets.mkdir()
        write_tone(assets / "a.wav", 0.5, 440.0)
        manifest = self.make_manifest(
            [{"path": "assets/a.wav", "start": 0.0, "fade_in": 0.0, "fade_out": 0.0}]
        )
        result = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report()["完整"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
