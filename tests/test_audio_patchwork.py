#!/usr/bin/env python3
"""audio_patchwork 的端到端与单元测试（仅标准库 unittest）。

运行::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import audio_patchwork as patchwork  # noqa: E402
import make_samples  # noqa: E402


def run_main(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = patchwork.main(argv)
    return code, buffer.getvalue()


def read_wav(path: Path) -> tuple[wave._Wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(handle.getnframes())
    return params, frames


def concatenated_bytes(paths: list[Path]) -> bytes:
    chunks = []
    for path in paths:
        _, frames = read_wav(path)
        chunks.append(frames)
    return b"".join(chunks)


class TemporaryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()
        self.out = self.root / "out"

    def add_clip(
        self,
        name: str,
        samples: list[float],
        *,
        sample_rate: int = make_samples.SAMPLE_RATE,
        sample_width: int = 2,
        channels: int = 1,
    ) -> Path:
        path = self.clips / name
        make_samples.write_wav(
            path,
            samples,
            sample_rate=sample_rate,
            sample_width=sample_width,
            channels=channels,
        )
        return path

    def write_manifest(self, document: dict, name: str = "manifest.json") -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def manifest_for(self, clip_names: list[str], **extra) -> Path:
        document = {"clips": clip_names, "output": "track.wav"}
        document.update(extra)
        return self.write_manifest(document)

    def assert_no_outputs(self) -> None:
        self.assertFalse(
            (self.out / "track.wav").exists(), "不应生成输出音轨"
        )
        self.assertFalse(
            (self.out / "report.json").exists(), "不应生成复核报告"
        )


# ---------------------------------------------------------------------------
# 清单错误
# ---------------------------------------------------------------------------


class ManifestFailureTests(TemporaryCase):
    def test_missing_manifest_reports_path_and_writes_nothing(self) -> None:
        missing = self.root / "nope.json"
        code, output = run_main([str(missing), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("清单文件不存在", output)
        self.assertIn(str(missing.resolve()), output)
        self.assertFalse(self.out.exists())

    def test_non_json_manifest_reports_error(self) -> None:
        bad = self.root / "manifest.json"
        bad.write_text("这不是 JSON，只是文本", encoding="utf-8")
        code, output = run_main([str(bad), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("无法解析清单 JSON", output)
        self.assertFalse(self.out.exists())

    def test_broken_json_reports_location(self) -> None:
        bad = self.root / "manifest.json"
        bad.write_text('{"clips": [', encoding="utf-8")
        code, output = run_main([str(bad), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("无法解析清单 JSON", output)
        self.assertIn("行", output)
        self.assertFalse(self.out.exists())

    def test_manifest_missing_clips_field(self) -> None:
        manifest = self.write_manifest({"output": "track.wav"})
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("缺少 clips 字段", output)
        self.assertFalse(self.out.exists())

    def test_manifest_clips_not_array(self) -> None:
        manifest = self.write_manifest(
            {"clips": "clips/a.wav", "output": "track.wav"}
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("clips 字段必须是数组", output)

    def test_manifest_clip_entry_not_string(self) -> None:
        manifest = self.write_manifest(
            {"clips": ["clips/a.wav", 7], "output": "track.wav"}
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("clips[1]", output)

    def test_manifest_missing_output(self) -> None:
        manifest = self.write_manifest({"clips": ["clips/a.wav"]})
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("output 字段", output)

    def test_manifest_root_not_object(self) -> None:
        manifest = self.write_manifest(["clips/a.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("顶层必须是 JSON 对象", output)

    def test_manifest_is_directory(self) -> None:
        code, output = run_main([str(self.root), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("清单路径不是文件", output)


# ---------------------------------------------------------------------------
# 片段错误
# ---------------------------------------------------------------------------


class ClipFailureTests(TemporaryCase):
    def test_sample_rate_mismatch_is_reported(self) -> None:
        self.add_clip("ok.wav", make_samples.tone(0.1, 440.0))
        self.add_clip(
            "wrong.wav", make_samples.tone(0.1, 440.0), sample_rate=16000
        )
        manifest = self.manifest_for(["clips/ok.wav", "clips/wrong.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("采样率不匹配", output)
        self.assertIn("16000 Hz", output)
        self.assertIn("8000 Hz", output)
        self.assertIn(str((self.clips / "wrong.wav").resolve()), output)
        self.assert_no_outputs()

    def test_bit_depth_mismatch_is_reported(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0), sample_width=2)
        self.add_clip("b.wav", make_samples.tone(0.1, 440.0), sample_width=1)
        manifest = self.manifest_for(["clips/a.wav", "clips/b.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("位深不匹配", output)
        self.assertIn("8 位", output)
        self.assertIn("16 位", output)
        self.assert_no_outputs()

    def test_channel_mismatch_is_reported(self) -> None:
        self.add_clip("mono.wav", make_samples.tone(0.1, 440.0))
        self.add_clip(
            "stereo.wav",
            make_samples.tones(0.1, (440.0, 660.0)),
            channels=2,
        )
        manifest = self.manifest_for(["clips/mono.wav", "clips/stereo.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("声道数不匹配", output)
        self.assertIn("期望 1", output)
        self.assert_no_outputs()

    def test_missing_clip_reports_path_and_writes_nothing(self) -> None:
        self.add_clip("present.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/present.wav", "clips/absent.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("片段文件不存在", output)
        self.assertIn(str((self.clips / "absent.wav").resolve()), output)
        self.assert_no_outputs()

    def test_non_wav_clip_is_rejected(self) -> None:
        garbage = self.clips / "garbage.wav"
        garbage.write_bytes(b"not a wave file")
        manifest = self.manifest_for(["clips/garbage.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("无法读取 PCM WAV 片段", output)
        self.assert_no_outputs()

    def test_truncated_wav_is_rejected(self) -> None:
        path = self.add_clip("trunc.wav", make_samples.tone(0.1, 440.0))
        data = path.read_bytes()
        path.write_bytes(data[:-40])
        manifest = self.manifest_for(["clips/trunc.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("数据不完整", output)
        self.assert_no_outputs()


# ---------------------------------------------------------------------------
# 拼接正确性
# ---------------------------------------------------------------------------


class ConcatenationTests(TemporaryCase):
    def test_two_clips_concatenate_in_order(self) -> None:
        first = self.add_clip("first.wav", make_samples.tone(0.25, 440.0))
        second = self.add_clip("second.wav", make_samples.tone(0.50, 660.0))
        manifest = self.manifest_for(["clips/first.wav", "clips/second.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)

        params, frames = read_wav(self.out / "track.wav")
        first_params, first_frames = read_wav(first)
        second_params, second_frames = read_wav(second)

        self.assertEqual(params.nchannels, 1)
        self.assertEqual(params.sampwidth, 2)
        self.assertEqual(params.framerate, make_samples.SAMPLE_RATE)
        self.assertEqual(params.nframes, first_params.nframes + second_params.nframes)
        self.assertEqual(frames, first_frames + second_frames)
        self.assertAlmostEqual(
            params.nframes / params.framerate, 0.25 + 0.50, places=6
        )

    def test_duration_matches_sum_in_report(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.2, 440.0))
        self.add_clip("b.wav", make_samples.tone(0.3, 550.0))
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/b.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(report["total_duration"], 0.5)

    def test_mono_channel_count_preserved(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        self.add_clip("b.wav", make_samples.tone(0.1, 660.0))
        manifest = self.manifest_for(["clips/a.wav", "clips/b.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        params, _ = read_wav(self.out / "track.wav")
        self.assertEqual(params.nchannels, 1)

    def test_stereo_interleaving_preserved(self) -> None:
        first = self.add_clip(
            "left_right_a.wav",
            make_samples.tones(0.05, (440.0, 880.0)),
            channels=2,
        )
        second = self.add_clip(
            "left_right_b.wav",
            make_samples.tones(0.05, (330.0, 550.0)),
            channels=2,
        )
        manifest = self.manifest_for(
            ["clips/left_right_a.wav", "clips/left_right_b.wav"]
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        params, frames = read_wav(self.out / "track.wav")
        self.assertEqual(params.nchannels, 2)
        self.assertEqual(frames, concatenated_bytes([first, second]))
        # 逐帧检查左右交替：奇数下标为右声道（880/550Hz），偶数下标为左声道。
        samples = [
            int.from_bytes(frames[i : i + 2], "little", signed=True)
            for i in range(0, len(frames), 2)
        ]
        left_first = samples[0::2][:40]
        self.assertEqual(len(samples) % 2, 0)
        # 左右不同频率，因此前 40 个左声道样本不应与右声道完全相同。
        self.assertNotEqual(samples[0::2][:40], samples[1::2][:40])
        self.assertTrue(any(value != 0 for value in left_first))

    def test_8bit_unsigned_samples_preserved(self) -> None:
        first = self.add_clip(
            "a.wav", make_samples.tone(0.1, 440.0), sample_width=1
        )
        second = self.add_clip(
            "b.wav", make_samples.tone(0.1, 660.0), sample_width=1
        )
        manifest = self.manifest_for(["clips/a.wav", "clips/b.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        params, frames = read_wav(self.out / "track.wav")
        self.assertEqual(params.sampwidth, 1)
        self.assertEqual(frames, concatenated_bytes([first, second]))

    def test_24bit_samples_preserved(self) -> None:
        first = self.add_clip(
            "a.wav", make_samples.tone(0.05, 440.0), sample_width=3
        )
        second = self.add_clip(
            "b.wav", make_samples.tone(0.05, 660.0), sample_width=3
        )
        manifest = self.manifest_for(["clips/a.wav", "clips/b.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        params, frames = read_wav(self.out / "track.wav")
        self.assertEqual(params.sampwidth, 3)
        self.assertEqual(frames, concatenated_bytes([first, second]))


# ---------------------------------------------------------------------------
# 静音检测
# ---------------------------------------------------------------------------


class SilenceTests(TemporaryCase):
    def _run_silence_scene(self, extra_argv: list[str] | None = None):
        self.add_clip("before.wav", make_samples.tone(0.25, 440.0))
        self.add_clip("gap.wav", make_samples.silence(0.5))
        self.add_clip("after.wav", make_samples.tone(0.25, 440.0))
        manifest = self.manifest_for(
            ["clips/before.wav", "clips/gap.wav", "clips/after.wav"],
            report="report.json",
        )
        argv = [str(manifest), "-o", str(self.out)] + (extra_argv or [])
        code, output = run_main(argv)
        self.assertEqual(code, 0, output)
        return json.loads((self.out / "report.json").read_text("utf-8")), output

    def test_half_second_silence_reported_with_three_decimals(self) -> None:
        report, _ = self._run_silence_scene()
        self.assertEqual(report["total_duration"], 1.0)
        self.assertEqual(len(report["silence_segments"]), 1)
        segment = report["silence_segments"][0]
        self.assertEqual(segment["start"], 0.25)
        self.assertEqual(segment["end"], 0.75)
        self.assertEqual(segment["duration"], 0.5)

    def test_silence_times_are_rounded_to_three_decimals(self) -> None:
        report, _ = self._run_silence_scene()
        for segment in report["silence_segments"]:
            for key in ("start", "end", "duration"):
                text = f"{segment[key]:.3f}"
                self.assertEqual(float(text), segment[key])
                self.assertAlmostEqual(segment[key], round(segment[key], 3))

    def test_no_silence_yields_empty_list_not_null(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.2, 440.0))
        self.add_clip("b.wav", make_samples.tone(0.3, 660.0))
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/b.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertIsInstance(report["silence_segments"], list)
        self.assertEqual(report["silence_segments"], [])
        self.assertEqual(report["total_duration"], 0.5)
        self.assertIn("静音段数: 0", output)

    def test_trailing_silence_detected_to_end(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.2, 440.0))
        self.add_clip("tail.wav", make_samples.silence(0.3))
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/tail.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(
            report["silence_segments"],
            [{"start": 0.2, "end": 0.5, "duration": 0.3}],
        )

    def test_whole_track_silence_detected(self) -> None:
        self.add_clip("gap.wav", make_samples.silence(0.4))
        manifest = self.manifest_for(
            ["clips/gap.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(
            report["silence_segments"],
            [{"start": 0.0, "end": 0.4, "duration": 0.4}],
        )

    def test_short_gap_below_min_duration_ignored(self) -> None:
        # 0.05 秒静音 < 默认 0.1 秒最短时长，不应报告。
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        self.add_clip("gap.wav", make_samples.silence(0.05))
        self.add_clip("b.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/gap.wav", "clips/b.wav"],
            report="report.json",
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(report["silence_segments"], [])

    def test_custom_min_silence_changes_result(self) -> None:
        # 0.5 秒静音在 --min-silence 0.6 下不报告，在 0.5 下报告。
        report, _ = self._run_silence_scene(["--min-silence", "0.6"])
        self.assertEqual(report["silence_segments"], [])

        self.out = self.root / "out2"
        report, _ = self._run_silence_scene(["--min-silence", "0.5"])
        self.assertEqual(len(report["silence_segments"]), 1)
        self.assertEqual(report["silence_segments"][0]["duration"], 0.5)

    def test_custom_threshold_changes_result(self) -> None:
        # 低幅度（0.05）片段：阈值 0.1 时整段静音，默认 0.01 时不报告。
        self.add_clip("quiet.wav", make_samples.tone(0.3, 440.0, amplitude=0.05))
        self.add_clip("loud.wav", make_samples.tone(0.3, 440.0, amplitude=0.5))
        manifest = self.manifest_for(
            ["clips/quiet.wav", "clips/loud.wav"], report="report.json"
        )
        code, output = run_main(
            [str(manifest), "-o", str(self.out), "--threshold", "0.1"]
        )
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(
            report["silence_segments"],
            [{"start": 0.0, "end": 0.3, "duration": 0.3}],
        )

        self.out = self.root / "out2"
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(report["silence_segments"], [])

    def test_custom_threshold_and_min_silence_are_echoed(self) -> None:
        report, _ = self._run_silence_scene(
            ["--threshold", "0.02", "--min-silence", "0.25"]
        )
        self.assertEqual(report["amplitude_threshold"], 0.02)
        self.assertEqual(report["min_silence_duration"], 0.25)

    def test_silence_requires_all_channels_quiet(self) -> None:
        # 左声道有声、右声道静音，不应判为静音段。
        samples = []
        tone_values = make_samples.tone(0.3, 440.0)
        for value in tone_values:
            samples.extend([value, 0.0])
        self.add_clip("half.wav", samples, channels=2)
        manifest = self.manifest_for(
            ["clips/half.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(report["silence_segments"], [])
        self.assertEqual(report["channels"], 2)

    def test_8bit_silence_detected(self) -> None:
        self.add_clip("gap.wav", make_samples.silence(0.3), sample_width=1)
        manifest = self.manifest_for(
            ["clips/gap.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(
            report["silence_segments"],
            [{"start": 0.0, "end": 0.3, "duration": 0.3}],
        )

    def test_24bit_silence_detected(self) -> None:
        self.add_clip("gap.wav", make_samples.silence(0.3), sample_width=3)
        manifest = self.manifest_for(
            ["clips/gap.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        report = json.loads((self.out / "report.json").read_text("utf-8"))
        self.assertEqual(
            report["silence_segments"],
            [{"start": 0.0, "end": 0.3, "duration": 0.3}],
        )

    def test_invalid_threshold_rejected(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        for value in ("-0.1", "1.5"):
            code, output = run_main(
                [str(manifest), "-o", str(self.out), "--threshold", value]
            )
            self.assertEqual(code, 1)
            self.assertIn("幅度阈值必须在 0 到 1 之间", output)
            self.assert_no_outputs()

    def test_invalid_min_silence_rejected(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        code, output = run_main(
            [str(manifest), "-o", str(self.out), "--min-silence", "0"]
        )
        self.assertEqual(code, 1)
        self.assertIn("最短静音时长必须大于 0 秒", output)
        self.assert_no_outputs()


# ---------------------------------------------------------------------------
# 空列表 / 输出冲突 / 原子性
# ---------------------------------------------------------------------------


class EmptyAndOutputTests(TemporaryCase):
    def test_empty_clip_list_prints_required_message(self) -> None:
        manifest = self.write_manifest(
            {"clips": [], "output": "track.wav", "report": "report.json"}
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("无片段可拼接", output)
        self.assertFalse(self.out.exists())

    def test_existing_output_is_not_overwritten(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        self.out.mkdir()
        existing = self.out / "track.wav"
        original = b"ORIGINAL-OUTPUT-MUST-SURVIVE"
        existing.write_bytes(original)

        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("输出音轨已存在，拒绝覆盖", output)
        self.assertIn(str(existing.resolve()), output)
        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse((self.out / "report.json").exists())

    def test_existing_output_directory_named_like_file(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        (self.out / "track.wav").mkdir(parents=True)
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("输出音轨已存在", output)

    def test_format_error_keeps_existing_report_unchanged(self) -> None:
        self.add_clip("ok.wav", make_samples.tone(0.1, 440.0))
        self.add_clip(
            "wrong.wav", make_samples.tone(0.1, 440.0), sample_rate=16000
        )
        self.out.mkdir()
        report_path = self.out / "report.json"
        original = b'{"sentinel": true}'
        report_path.write_bytes(original)

        manifest = self.manifest_for(
            ["clips/ok.wav", "clips/wrong.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertEqual(report_path.read_bytes(), original)
        self.assertFalse((self.out / "track.wav").exists())

    def test_failure_creates_no_report_when_absent(self) -> None:
        self.add_clip("ok.wav", make_samples.tone(0.1, 440.0))
        self.add_clip(
            "wrong.wav", make_samples.tone(0.1, 440.0), sample_width=3
        )
        manifest = self.manifest_for(
            ["clips/ok.wav", "clips/wrong.wav"], report="report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assert_no_outputs()

    def test_invalid_report_parent_creates_no_audio(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        self.out.mkdir()
        blocker = self.out / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        manifest = self.manifest_for(
            ["clips/a.wav"], report="blocked/report.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("输出目录不是目录", output)
        self.assertFalse((self.out / "track.wav").exists())
        self.assertEqual(blocker.read_text(encoding="utf-8"), "not a directory")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["blocked"])

    def test_report_cannot_overwrite_manifest(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(
            ["clips/a.wav"], report="manifest.json"
        )
        code, output = run_main([str(manifest), "-o", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("不能覆盖清单文件", output)
        self.assertTrue(manifest.exists())

    def test_report_cannot_equal_audio_path(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(
            ["clips/a.wav"], output="out/same.wav", report="out/same.wav"
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("不能与输出音轨路径相同", output)
        self.assertFalse((self.out / "same.wav").exists())

    def test_report_cannot_overwrite_clip(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        clip = self.clips / "a.wav"
        original = clip.read_bytes()
        manifest = self.manifest_for(
            ["clips/a.wav"], report="clips/a.wav"
        )
        code, output = run_main([str(manifest), "-o", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("不能覆盖输入片段", output)
        self.assertEqual(clip.read_bytes(), original)
        self.assertFalse((self.root / "out" / "track.wav").exists())

    def test_nested_output_directories_created(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(
            ["clips/a.wav"],
            output="deep/nested/track.wav",
            report="deep/nested/report.json",
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        self.assertTrue((self.out / "deep" / "nested" / "track.wav").is_file())
        self.assertTrue((self.out / "deep" / "nested" / "report.json").is_file())


# ---------------------------------------------------------------------------
# stdout 契约与 CLI
# ---------------------------------------------------------------------------


class CliTests(TemporaryCase):
    def test_success_stdout_contract(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.2, 440.0))
        self.add_clip("b.wav", make_samples.tone(0.2, 660.0))
        self.add_clip("gap.wav", make_samples.silence(0.2))
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/b.wav", "clips/gap.wav"],
            report="report.json",
        )
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("片段数: 3", output)
        self.assertIn(f"输出: {(self.out / 'track.wav').resolve()}", output)
        self.assertIn("静音段数: 1", output)

    def test_default_report_name_matches_audio(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        code, output = run_main([str(manifest), "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        self.assertTrue((self.out / "track_report.json").is_file())

    def test_manifest_relative_clips_resolve_from_manifest_dir(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        manifest = self.manifest_for(["clips/a.wav"])
        other_cwd = self.root / "elsewhere"
        other_cwd.mkdir()
        previous = Path.cwd()
        os.chdir(other_cwd)
        try:
            code, output = run_main([str(manifest), "-o", str(self.out)])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, output)
        self.assertTrue((self.out / "track.wav").is_file())


class SubprocessCliTests(TemporaryCase):
    """验证真实命令行行为（含默认输出目录 = 当前工作目录）。"""

    def run_cli(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "audio_patchwork.py"), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    def test_default_output_dir_is_current_working_directory(self) -> None:
        self.add_clip("a.wav", make_samples.tone(0.1, 440.0))
        self.add_clip("b.wav", make_samples.tone(0.1, 660.0))
        # 清单中的 output 写相对路径，运行目录另行指定。
        manifest = self.manifest_for(
            ["clips/a.wav", "clips/b.wav"], output="track.wav"
        )
        work = self.root / "work"
        work.mkdir()
        result = self.run_cli([str(manifest)], cwd=work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((work / "track.wav").is_file())
        self.assertTrue((work / "track_report.json").is_file())
        self.assertIn("片段数: 2", result.stdout)
        self.assertIn(str((work / "track.wav").resolve()), result.stdout)
        self.assertIn("静音段数: 0", result.stdout)

    def test_cli_missing_manifest_exit_code_and_no_output(self) -> None:
        work = self.root / "work"
        work.mkdir()
        result = self.run_cli([str(self.root / "absent.json")], cwd=work)
        self.assertEqual(result.returncode, 1)
        self.assertIn("清单文件不存在", result.stdout)
        self.assertEqual(list(work.iterdir()), [])

    def test_cli_empty_list_message(self) -> None:
        manifest = self.write_manifest({"clips": [], "output": "track.wav"})
        work = self.root / "work"
        work.mkdir()
        result = self.run_cli([str(manifest)], cwd=work)
        self.assertEqual(result.returncode, 2)
        self.assertIn("无片段可拼接", result.stdout)
        self.assertEqual(list(work.iterdir()), [])

    def test_cli_help_documents_defaults(self) -> None:
        result = self.run_cli(["--help"], cwd=self.root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--threshold", result.stdout)
        self.assertIn("0.01", result.stdout)
        self.assertIn("--min-silence", result.stdout)
        self.assertIn("0.1", result.stdout)
        self.assertIn("--output-dir", result.stdout)


# ---------------------------------------------------------------------------
# 单元级：静音算法与样本生成器
# ---------------------------------------------------------------------------


class UnitTests(unittest.TestCase):
    def test_detect_silence_on_handcrafted_frames(self) -> None:
        # 8000 Hz 单声道 16bit：5 帧有声 + 800 帧静音 + 5 帧有声。
        wav_format = patchwork.WavFormat(channels=1, sample_width=2, frame_rate=8000)
        frames = (
            (1000).to_bytes(2, "little", signed=True) * 5
            + b"\x00\x00" * 800
            + (1000).to_bytes(2, "little", signed=True) * 5
        )
        segments = patchwork.detect_silence(frames, wav_format, 0.01, 0.1)
        self.assertEqual(
            segments, [{"start": 0.001, "end": 0.101, "duration": 0.1}]
        )

    def test_detect_silence_excludes_exactly_at_threshold(self) -> None:
        # 阈值 0.5 -> 16384；样本恰为 16384 视为“不低于阈值”，不算静音。
        wav_format = patchwork.WavFormat(channels=1, sample_width=2, frame_rate=8000)
        frames = (16384).to_bytes(2, "little", signed=True) * 800
        self.assertEqual(
            patchwork.detect_silence(frames, wav_format, 0.5, 0.1), []
        )

    def test_detect_silence_empty_frames(self) -> None:
        wav_format = patchwork.WavFormat(channels=2, sample_width=2, frame_rate=8000)
        self.assertEqual(patchwork.detect_silence(b"", wav_format, 0.01, 0.1), [])

    def test_manifest_relative_resolution(self) -> None:
        manifest = patchwork.Manifest(
            path=Path("C:/demo/manifest.json"),
            clips=("clips/a.wav",),
            output="track.wav",
            report=None,
        )
        self.assertEqual(
            patchwork.resolve_clip_path(manifest, "clips/a.wav"),
            Path("C:/demo/clips/a.wav"),
        )

    def test_sample_generator_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = make_samples.build_all(Path(tmp) / "a")
            second = make_samples.build_all(Path(tmp) / "b")
            for path in sorted(first.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(first)
                    twin = second / relative
                    self.assertTrue(twin.is_file(), relative)
                    self.assertEqual(path.read_bytes(), twin.read_bytes(), relative)


if __name__ == "__main__":
    unittest.main(verbosity=2)
