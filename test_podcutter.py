#!/usr/bin/env python3
"""podcutter 的确定性测试套件（仅标准库）。

运行:
    python -m unittest -v test_podcutter.py
或:
    python test_podcutter.py
"""

from __future__ import annotations

import array
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podcutter  # noqa: E402


SAMPLE_RATE = 8000


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------

def make_samples(count: int) -> list[int]:
    """确定性的 16 位波形：周期 4000 的锯齿，覆盖正负值。"""
    return [((i * 37) % 4000) - 2000 for i in range(count)]


def write_pcm_wav(
    path: Path,
    samples: list[int],
    *,
    rate: int = SAMPLE_RATE,
    channels: int = 1,
    sampwidth: int = 2,
) -> None:
    raw = array.array("h", samples)
    if sys.byteorder != "little":
        raw.byteswap()
    payload = raw.tobytes()
    if sampwidth != 2:
        # 非 16 位场景：把 16 位样本线性缩放到目标字节宽度。
        if sampwidth == 1:
            payload = bytes(max(0, min(255, (s >> 8) + 128)) for s in samples)
        else:
            raise ValueError("测试只模拟 8 位或 16 位")
    elif channels != 1:
        # 把单声道样本复制到所有声道，保证合法多声道文件。
        frames = array.array("h")
        for sample in samples:
            frames.extend([sample] * channels)
        if sys.byteorder != "little":
            frames.byteswap()
        payload = frames.tobytes()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(payload)


def read_pcm_wav(path: Path) -> tuple[list[int], int, int]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return list(samples), rate, channels


def write_cuts(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def write_float_wav(path: Path, count: int) -> None:
    """手工构造 IEEE float32 WAV：wave 模块应拒绝并归类为不支持的格式。"""
    import struct

    payload = struct.pack("<%df" % count, *([0.25] * count))
    fmt = struct.pack(
        "<HHIIHH", 3, 1, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 32
    )
    with path.open("wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(payload))
                 + b"WAVE")
        fh.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        fh.write(b"data" + struct.pack("<I", len(payload)) + payload)


def run_tool(tmp: Path, cuts_data: object, *, wav_samples: list[int] | None = None,
             wav_name: str = "episode.wav", out_name: str = "clips") -> tuple[int, dict, Path, Path]:
    wav = tmp / wav_name
    if wav_samples is not None:
        write_pcm_wav(wav, wav_samples)
    cuts = tmp / "cuts.json"
    write_cuts(cuts, cuts_data)
    out = tmp / out_name
    report = tmp / "report.json"
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = podcutter.main(
            ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out), "--report", str(report)]
        )
    if not report.exists():
        raise AssertionError(f"报告未被写出；stdout=\n{buf.getvalue()}")
    return code, json.loads(report.read_text(encoding="utf-8")), out, report


def list_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir())


class PodcutterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assert_out_dir_empty(self, out: Path) -> None:
        self.assertEqual(list_files(out), [], "输出目录应当为空")


# ---------------------------------------------------------------------------
# 需求 1：输入格式校验
# ---------------------------------------------------------------------------

class InputFormatTests(PodcutterTestBase):
    def test_stereo_wav_is_unsupported_and_no_output(self) -> None:
        wav = self.tmp / "stereo.wav"
        write_pcm_wav(wav, make_samples(800), channels=2)
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 0.05}])
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"

        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )

        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error"]["code"], "UNSUPPORTED_WAV_FORMAT")
        self.assertIn("单声道", report["error"]["message"])
        self.assertIn("16 位", report["error"]["message"])
        self.assertEqual(len(report["segments"]), 1)
        self.assertEqual(report["segments"][0]["status"], "skipped")
        self.assertEqual(report["segments"][0]["output_file"], None)
        self.assert_out_dir_empty(out)

    def test_8bit_wav_is_unsupported_and_no_output(self) -> None:
        wav = self.tmp / "eight.wav"
        write_pcm_wav(wav, make_samples(800), sampwidth=1)
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 0.05}])
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"

        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )

        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["error"]["code"], "UNSUPPORTED_WAV_FORMAT")
        self.assert_out_dir_empty(out)

    def test_unsupported_format_does_not_leave_output_dir(self) -> None:
        wav = self.tmp / "stereo.wav"
        write_pcm_wav(wav, make_samples(800), channels=2)
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 0.05}])
        out = self.tmp / "clips"

        with contextlib.redirect_stdout(io.StringIO()):
            podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(self.tmp / "report.json")]
            )

        self.assertFalse(out.exists(), "格式错误时不应创建输出目录")

    def test_float_wav_is_unsupported(self) -> None:
        wav = self.tmp / "float.wav"
        write_float_wav(wav, 800)
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 0.05}])
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"

        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )

        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["error"]["code"], "UNSUPPORTED_WAV_FORMAT")
        self.assertEqual(report["segments"][0]["status"], "skipped")
        self.assert_out_dir_empty(out)


# ---------------------------------------------------------------------------
# 需求 2：片段范围校验
# ---------------------------------------------------------------------------

class RangeValidationTests(PodcutterTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.samples = make_samples(SAMPLE_RATE * 4)  # 4 秒

    def test_start_after_end_is_invalid_but_others_succeed(self) -> None:
        code, report, out, _ = run_tool(
            self.tmp,
            [
                {"name": "backwards", "start": 5, "end": 3},
                {"name": "ok", "start": 0, "end": 1},
            ],
            wav_samples=self.samples,
        )
        self.assertEqual(code, podcutter.EXIT_PARTIAL)
        self.assertEqual(report["status"], "partial")
        by_name = {segment["name"]: segment for segment in report["segments"]}
        self.assertEqual(by_name["backwards"]["status"], "invalid")
        self.assertIn("start", by_name["backwards"]["error"])
        self.assertIsNone(by_name["backwards"]["output_file"])
        self.assertEqual(by_name["ok"]["status"], "success")
        self.assertOutFileMatches(out, "ok", self.samples, 0, SAMPLE_RATE)

    def test_end_beyond_duration_is_invalid(self) -> None:
        code, report, out, _ = run_tool(
            self.tmp,
            [{"name": "toolong", "start": 3, "end": 5}],
            wav_samples=self.samples,
        )
        self.assertEqual(code, podcutter.EXIT_PARTIAL)
        segment = report["segments"][0]
        self.assertEqual(segment["status"], "invalid")
        self.assertIn("文件时长", segment["error"])
        self.assertNotIn("toolong.wav", list_files(out))

    def test_exact_file_duration_is_accepted(self) -> None:
        code, report, out, _ = run_tool(
            self.tmp,
            [{"name": "full", "start": 0, "end": 4}],
            wav_samples=self.samples,
        )
        self.assertEqual(code, podcutter.EXIT_OK)
        self.assertEqual(report["segments"][0]["duration_seconds"], 4.0)
        samples, _, _ = read_pcm_wav(out / "full.wav")
        self.assertEqual(samples, self.samples)

    def test_negative_start_is_invalid(self) -> None:
        code, report, out, _ = run_tool(
            self.tmp,
            [{"name": "neg", "start": -1, "end": 2}],
            wav_samples=self.samples,
        )
        self.assertEqual(code, podcutter.EXIT_PARTIAL)
        self.assertEqual(report["segments"][0]["status"], "invalid")
        self.assert_out_dir_empty(out)

    def test_zero_length_range_is_invalid(self) -> None:
        code, report, out, _ = run_tool(
            self.tmp,
            [{"name": "zero", "start": 1, "end": 1}],
            wav_samples=self.samples,
        )
        self.assertEqual(report["segments"][0]["status"], "invalid")
        self.assert_out_dir_empty(out)

    def test_sub_sample_range_is_invalid(self) -> None:
        # 8 kHz 下 0.00001 秒远小于一个采样点，且开始等于结束的换算结果。
        code, report, out, _ = run_tool(
            self.tmp,
            [{"name": "tiny", "start": 0.0, "end": 0.00001}],
            wav_samples=self.samples,
        )
        self.assertEqual(report["segments"][0]["status"], "invalid")
        self.assertOutDirEmpty(out)

    def assertOutFileMatches(self, out: Path, name: str, samples: list[int],
                             start: int, end: int) -> None:
        actual, rate, channels = read_pcm_wav(out / f"{name}.wav")
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(channels, 1)
        self.assertEqual(actual, samples[start:end])

    assertOutDirEmpty = PodcutterTestBase.assert_out_dir_empty


# ---------------------------------------------------------------------------
# 需求 3：采样与增益
# ---------------------------------------------------------------------------

class SamplingAndGainTests(PodcutterTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.samples = make_samples(SAMPLE_RATE * 2)  # 2 秒

    def test_zero_gain_is_sample_exact(self) -> None:
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "exact", "start": 0.5, "end": 1.25}],
            wav_samples=self.samples,
        )
        self.assertEqual(report["segments"][0]["status"], "success")
        expected = self.samples[4000:10000]
        actual, rate, channels = read_pcm_wav(out / "exact.wav")
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(channels, 1)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(actual, expected)

    def test_boundary_samples_are_exact(self) -> None:
        # 采样边界按 round(秒 * 采样率) 计算：0.1231 -> 984.8 -> 985。
        start, end = 0.1231, 0.4567
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "bounds", "start": start, "end": end}],
            wav_samples=self.samples,
        )
        expected_start = round(start * SAMPLE_RATE)
        expected_end = round(end * SAMPLE_RATE)
        actual, _, _ = read_pcm_wav(out / "bounds.wav")
        self.assertEqual(actual, self.samples[expected_start:expected_end])
        self.assertEqual(actual[0], self.samples[expected_start])
        self.assertEqual(actual[-1], self.samples[expected_end - 1])
        self.assertEqual(report["segments"][0]["start_sample"], expected_start)
        self.assertEqual(report["segments"][0]["end_sample"], expected_end)

    def test_positive_gain_scales_samples(self) -> None:
        # 取一段幅度较小的波形：-2000..1999，+20 dB 后为 -20000..19990，不裁剪。
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "loud", "start": 0.5, "end": 0.75, "gain": 20}],
            wav_samples=self.samples,
        )
        expected = [sample * 10 for sample in self.samples[4000:6000]]
        actual, _, _ = read_pcm_wav(out / "loud.wav")
        self.assertEqual(actual, expected)

    def test_negative_gain_scales_samples(self) -> None:
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "quiet", "start": 0.5, "end": 0.75, "gain": -20}],
            wav_samples=self.samples,
        )
        expected = [round(sample * 0.1) for sample in self.samples[4000:6000]]
        actual, _, _ = read_pcm_wav(out / "quiet.wav")
        self.assertEqual(actual, expected)

    def test_gain_clips_to_16bit_range(self) -> None:
        loud_samples = [20000, -20000, 32767, -32768, 0, 1000]
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "clip", "start": 0, "end": len(loud_samples) / SAMPLE_RATE,
              "gain": 20}],
            wav_samples=loud_samples,
        )
        actual, _, _ = read_pcm_wav(out / "clip.wav")
        self.assertEqual(actual, [32767, -32768, 32767, -32768, 0, 10000])

    def test_default_gain_is_zero(self) -> None:
        _, report, out, _ = run_tool(
            self.tmp,
            [{"name": "plain", "start": 0, "end": 0.25}],
            wav_samples=self.samples,
        )
        self.assertEqual(report["segments"][0]["gain_db"], 0.0)
        actual, _, _ = read_pcm_wav(out / "plain.wav")
        self.assertEqual(actual, self.samples[0:2000])

    def test_output_wav_header(self) -> None:
        _, _, out, _ = run_tool(
            self.tmp,
            [{"name": "header", "start": 0, "end": 1}],
            wav_samples=self.samples,
        )
        with wave.open(str(out / "header.wav"), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), SAMPLE_RATE)
            self.assertEqual(wf.getnframes(), SAMPLE_RATE)


# ---------------------------------------------------------------------------
# 需求 4 / 6：整体配置校验
# ---------------------------------------------------------------------------

class ConfigValidationTests(PodcutterTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.samples = make_samples(SAMPLE_RATE)

    def assert_config_error(self, cuts_data: object, expected_fragment: str) -> dict:
        code, report, out, _ = run_tool(
            self.tmp, cuts_data, wav_samples=self.samples
        )
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["status"], "error")
        self.assertIn(report["error"]["code"], {"CONFIG_ERROR", "INVALID_JSON"})
        self.assertIn(expected_fragment, report["error"]["message"])
        self.assertEqual(report["segments"], [])
        self.assert_out_dir_empty(out)
        self.assertFalse(out.exists(), "配置错误时不应创建输出目录")
        return report

    def test_illegal_characters_in_name(self) -> None:
        self.assert_config_error(
            [
                {"name": "good", "start": 0, "end": 0.1},
                {"name": "bad-name", "start": 0, "end": 0.1},
            ],
            "非法",
        )

    def test_chinese_name_is_illegal(self) -> None:
        self.assert_config_error(
            [{"name": "第一段", "start": 0, "end": 0.1}], "非法"
        )

    def test_space_in_name_is_illegal(self) -> None:
        self.assert_config_error(
            [{"name": "my clip", "start": 0, "end": 0.1}], "非法"
        )

    def test_empty_name_is_illegal(self) -> None:
        self.assert_config_error(
            [{"name": "", "start": 0, "end": 0.1}], "非法"
        )

    def test_duplicate_names(self) -> None:
        self.assert_config_error(
            [
                {"name": "same", "start": 0, "end": 0.1},
                {"name": "same", "start": 0.1, "end": 0.2},
            ],
            "重复",
        )

    def test_missing_end_field(self) -> None:
        self.assert_config_error(
            [{"name": "noend", "start": 0}], "缺少必需字段"
        )

    def test_missing_start_field(self) -> None:
        self.assert_config_error(
            [{"name": "nostart", "end": 1}], "缺少必需字段"
        )

    def test_missing_name_field(self) -> None:
        self.assert_config_error(
            [{"start": 0, "end": 1}], "缺少必需字段"
        )

    def test_start_as_string(self) -> None:
        self.assert_config_error(
            [{"name": "s", "start": "0.0", "end": 1}], "必须是有限数字"
        )

    def test_end_as_null(self) -> None:
        self.assert_config_error(
            [{"name": "s", "start": 0, "end": None}], "必须是有限数字"
        )

    def test_name_as_number(self) -> None:
        self.assert_config_error(
            [{"name": 123, "start": 0, "end": 1}], "必须是字符串"
        )

    def test_bool_start_is_rejected(self) -> None:
        self.assert_config_error(
            [{"name": "s", "start": True, "end": 1}], "必须是有限数字"
        )

    def test_gain_as_string(self) -> None:
        self.assert_config_error(
            [{"name": "s", "start": 0, "end": 1, "gain": "3"}], "gain"
        )

    def test_non_finite_gain_is_rejected(self) -> None:
        # JSON 标准不允许 NaN，但 Python json 默认会接受；工具应将其视为配置错误。
        cuts = self.tmp / "cuts.json"
        cuts.write_text(
            '[{"name": "s", "start": 0, "end": 1, "gain": NaN}]',
            encoding="utf-8",
        )
        wav = self.tmp / "episode.wav"
        write_pcm_wav(wav, self.samples)
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["error"]["code"], "CONFIG_ERROR")
        self.assert_out_dir_empty(out)

    def test_item_not_object(self) -> None:
        self.assert_config_error(["not-an-object"], "必须是 JSON 对象")

    def test_top_level_not_list(self) -> None:
        self.assert_config_error({"name": "a", "start": 0, "end": 1}, "必须是数组")

    def test_malformed_json(self) -> None:
        cuts = self.tmp / "cuts.json"
        cuts.write_text('[{"name": "a", "start": 0, "end": 1}', encoding="utf-8")
        wav = self.tmp / "episode.wav"
        write_pcm_wav(wav, self.samples)
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["error"]["code"], "INVALID_JSON")
        self.assert_out_dir_empty(out)

    def test_empty_list_succeeds_with_no_segments(self) -> None:
        code, report, out, _ = run_tool(self.tmp, [], wav_samples=self.samples)
        self.assertEqual(code, podcutter.EXIT_OK)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["segments"], [])
        self.assertEqual(report["summary"]["total"], 0)
        self.assertTrue(out.is_dir())
        self.assert_out_dir_empty(out)


# ---------------------------------------------------------------------------
# 需求 5：覆盖同名旧文件
# ---------------------------------------------------------------------------

class OverwriteTests(PodcutterTestBase):
    def test_existing_output_is_overwritten(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        out = self.tmp / "clips"
        out.mkdir()
        stale = make_samples(123)
        write_pcm_wav(out / "intro.wav", stale, rate=16000)

        code, report, _, _ = run_tool(
            self.tmp,
            [{"name": "intro", "start": 0.25, "end": 0.5}],
            wav_samples=samples,
        )
        self.assertEqual(code, podcutter.EXIT_OK)
        self.assertEqual(report["segments"][0]["status"], "success")
        self.assertEqual(report["segments"][0]["output_file"], "intro.wav")
        actual, rate, channels = read_pcm_wav(out / "intro.wav")
        self.assertEqual(rate, SAMPLE_RATE)
        self.assertEqual(channels, 1)
        self.assertEqual(actual, samples[2000:4000])
        self.assertNotEqual(actual, stale)

    def test_overwrite_does_not_leave_temp_files(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        out = self.tmp / "clips"
        out.mkdir()
        write_pcm_wav(out / "a.wav", make_samples(10))
        run_tool(
            self.tmp,
            [{"name": "a", "start": 0, "end": 0.1}],
            wav_samples=samples,
        )
        self.assertEqual(list_files(out), ["a.wav"])


# ---------------------------------------------------------------------------
# 需求 7：单片段失败隔离
# ---------------------------------------------------------------------------

class FailureIsolationTests(PodcutterTestBase):
    def test_write_failure_is_isolated_and_reported(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        out = self.tmp / "clips"
        out.mkdir()
        # 用同名目录制造确定的写出失败（文件无法覆盖目录）。
        (out / "boom.wav").mkdir()

        code, report, _, _ = run_tool(
            self.tmp,
            [
                {"name": "first", "start": 0, "end": 0.25},
                {"name": "boom", "start": 0.25, "end": 0.5},
                {"name": "last", "start": 0.5, "end": 0.75},
            ],
            wav_samples=samples,
        )

        self.assertEqual(code, podcutter.EXIT_PARTIAL)
        self.assertEqual(report["status"], "partial")
        self.assertEqual([s["name"] for s in report["segments"]],
                         ["first", "boom", "last"])
        self.assertEqual(report["summary"], {
            "total": 3, "success": 2, "failed": 1, "invalid": 0, "skipped": 0,
        })

        failed = report["segments"][1]
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["output_file"])
        self.assertIsNone(failed["duration_seconds"])
        self.assertIn("写出片段失败", failed["error"])
        self.assertGreater(len(failed["error"]), len("写出片段失败"))

        # 成功片段不受污染，内容与手算一致。
        self.assertEqual(
            read_pcm_wav(out / "first.wav")[0], samples[0:2000]
        )
        self.assertEqual(
            read_pcm_wav(out / "last.wav")[0], samples[4000:6000]
        )
        # 失败的片段不应留下临时文件。
        self.assertEqual(
            sorted(p.name for p in out.iterdir()), ["boom.wav", "first.wav", "last.wav"]
        )

    def test_failure_then_invalid_then_success_order_preserved(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        out = self.tmp / "clips"
        out.mkdir()
        (out / "broken.wav").mkdir()

        _, report, _, _ = run_tool(
            self.tmp,
            [
                {"name": "broken", "start": 0, "end": 0.1},
                {"name": "oops", "start": 0.5, "end": 0.2},
                {"name": "fine", "start": 0.2, "end": 0.4, "gain": 0},
            ],
            wav_samples=samples,
        )
        self.assertEqual(
            [s["name"] for s in report["segments"]], ["broken", "oops", "fine"]
        )
        self.assertEqual(report["segments"][0]["status"], "failed")
        self.assertEqual(report["segments"][1]["status"], "invalid")
        self.assertEqual(report["segments"][2]["status"], "success")

    @unittest.skipUnless(os.name == "nt", "大小写冲突检测依赖不区分大小写的文件系统")
    def test_case_insensitive_output_collision_is_reported(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        code, report, out, _ = run_tool(
            self.tmp,
            [
                {"name": "Clip", "start": 0, "end": 0.1},
                {"name": "clip", "start": 0.1, "end": 0.2},
            ],
            wav_samples=samples,
        )
        self.assertEqual(code, podcutter.EXIT_PARTIAL)
        self.assertEqual(report["segments"][0]["status"], "success")
        self.assertEqual(report["segments"][1]["status"], "failed")
        self.assertIn("冲突", report["segments"][1]["error"])
        self.assertEqual(
            read_pcm_wav(out / "Clip.wav")[0], samples[0:800]
        )


# ---------------------------------------------------------------------------
# 需求 8：报告结构
# ---------------------------------------------------------------------------

class ReportSchemaTests(PodcutterTestBase):
    def test_report_fields_and_order(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        out = self.tmp / "clips"
        out.mkdir()
        (out / "broken.wav").mkdir()

        _, report, _, _ = run_tool(
            self.tmp,
            [
                {"name": "ok1", "start": 0, "end": 0.5},
                {"name": "bad", "start": 2, "end": 1},
                {"name": "broken", "start": 0.1, "end": 0.2},
                {"name": "ok2", "start": 0.5, "end": 0.75, "gain": 0},
            ],
            wav_samples=samples,
        )

        self.assertEqual(
            [s["name"] for s in report["segments"]],
            ["ok1", "bad", "broken", "ok2"],
        )
        for segment in report["segments"]:
            for key in ("name", "status", "output_file", "duration_seconds", "error"):
                self.assertIn(key, segment)
            self.assertIn(segment["status"], {"success", "failed", "invalid", "skipped"})
            if segment["status"] == "success":
                self.assertEqual(segment["output_file"], segment["name"] + ".wav")
                self.assertIsInstance(segment["duration_seconds"], float)
                self.assertIsNone(segment["error"])
            else:
                self.assertIsNone(segment["output_file"])
                self.assertIsNone(segment["duration_seconds"])
                self.assertTrue(segment["error"])

        ok1 = report["segments"][0]
        self.assertEqual(ok1["duration_seconds"], 0.5)
        ok2 = report["segments"][3]
        self.assertEqual(ok2["duration_seconds"], 0.25)
        self.assertEqual(report["summary"], {
            "total": 4, "success": 2, "failed": 1, "invalid": 1, "skipped": 0,
        })
        self.assertEqual(report["audio"]["sample_rate"], SAMPLE_RATE)
        self.assertEqual(report["audio"]["duration_seconds"], 1.0)
        self.assertEqual(report["inputs"]["output_dir"], str(out.resolve()))

    def test_report_is_valid_utf8_json_with_chinese_error(self) -> None:
        samples = make_samples(SAMPLE_RATE)
        _, report, _, report_path = run_tool(
            self.tmp,
            [{"name": "中文", "start": 0, "end": 1}],
            wav_samples=samples,
        )
        raw = report_path.read_bytes()
        raw.decode("utf-8")
        self.assertIn("非法", report["error"]["message"])


# ---------------------------------------------------------------------------
# 需求 9：输入文件存在性
# ---------------------------------------------------------------------------

class MissingInputTests(PodcutterTestBase):
    def _run(self, wav: Path, cuts: Path) -> tuple[int, dict, Path]:
        out = self.tmp / "clips"
        report_path = self.tmp / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = podcutter.main(
                ["--wav", str(wav), "--cuts", str(cuts), "--out", str(out),
                 "--report", str(report_path)]
            )
        return code, json.loads(report_path.read_text(encoding="utf-8")), out

    def test_missing_wav(self) -> None:
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 1}])
        code, report, out = self._run(self.tmp / "nope.wav", cuts)
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["error"]["code"], "FILE_NOT_FOUND")
        self.assertIn("不存在", report["error"]["message"])
        self.assertEqual(report["segments"], [])
        self.assert_out_dir_empty(out)

    def test_missing_cuts_json(self) -> None:
        wav = self.tmp / "episode.wav"
        write_pcm_wav(wav, make_samples(SAMPLE_RATE))
        code, report, out = self._run(wav, self.tmp / "nope.json")
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["error"]["code"], "FILE_NOT_FOUND")
        self.assertIn("裁剪列表", report["error"]["message"])
        self.assert_out_dir_empty(out)

    def test_both_missing_reports_both(self) -> None:
        code, report, out = self._run(self.tmp / "a.wav", self.tmp / "b.json")
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        message = report["error"]["message"]
        self.assertIn("a.wav", message)
        self.assertIn("b.json", message)
        self.assert_out_dir_empty(out)

    def test_directory_instead_of_file(self) -> None:
        cuts_dir = self.tmp / "cuts.json"
        cuts_dir.mkdir()
        wav = self.tmp / "episode.wav"
        write_pcm_wav(wav, make_samples(SAMPLE_RATE))
        code, report, out = self._run(wav, cuts_dir)
        self.assertEqual(code, podcutter.EXIT_TERMINAL)
        self.assertEqual(report["error"]["code"], "FILE_NOT_READABLE")
        self.assertIn("不是普通文件", report["error"]["message"])
        self.assert_out_dir_empty(out)


# ---------------------------------------------------------------------------
# 补充：API 层测试
# ---------------------------------------------------------------------------

class ApiTests(PodcutterTestBase):
    def test_cut_podcast_without_report_file(self) -> None:
        wav = self.tmp / "episode.wav"
        write_pcm_wav(wav, make_samples(SAMPLE_RATE))
        cuts = self.tmp / "cuts.json"
        write_cuts(cuts, [{"name": "a", "start": 0, "end": 0.1}])
        out = self.tmp / "clips"
        report = podcutter.cut_podcast(str(wav), str(cuts), str(out))
        self.assertEqual(report["status"], "success")
        self.assertTrue((out / "a.wav").exists())
        self.assertFalse((self.tmp / "report.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
