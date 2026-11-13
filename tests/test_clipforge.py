"""ClipForge 验收测试：覆盖 8 项验收要求与边界条件。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipforge.processor import run  # noqa: E402
from clipforge.wavio import verify_riff_structure  # noqa: E402
from tests.helpers import (  # noqa: E402
    clean_dir,
    decode,
    encode,
    expected_gain_sample,
    frame_at,
    frame_span,
    read_frames,
    write_non_pcm_wav,
    write_wav,
)

BASE_TMP = ROOT / ".tmp" / "tests"


def setUpModule() -> None:
    clean_dir(BASE_TMP)


def tearDownModule() -> None:
    clean_dir(BASE_TMP)


class ClipForgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = BASE_TMP / f"{self.__class__.__name__}-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.src = self.dir / "source.wav"
        self.manifest = self.dir / "manifest.json"
        self.out = self.dir / "out.wav"
        self.report = self.dir / "report.json"

    def tearDown(self) -> None:
        clean_dir(BASE_TMP)

    # ---------- helpers ----------
    def make_source(self, frames: int = 16000, sample_rate: int = 8000,
                    channels: int = 1, sample_width: int = 2,
                    seed: int = 0) -> None:
        write_wav(self.src, sample_rate, channels, sample_width, frames, seed=seed)

    def write_manifest(self, entries) -> None:
        self.manifest.write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def read_report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def run_ok(self) -> dict:
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertEqual(result.exit_code, 0, result.report.get("errors"))
        self.assertTrue(self.out.exists())
        return self.read_report()

    def source_params(self) -> dict:
        return read_frames(self.src)[0]

    # ---------- 要求 1：3 条合法项，报告逐项列出 ----------
    def test_req1_three_valid_entries_reported(self) -> None:
        self.make_source(frames=24000)
        self.write_manifest([
            {"start": 0.0, "end": 0.5},
            {"start": 0.5, "end": 1.0, "gain": 0.5},
            {"start": 1.0, "end": 1.5, "gain": 1.0},
        ])
        report = self.run_ok()
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["manifest"]["entry_count"], 3)
        self.assertEqual(len(report["clips"]), 3)
        self.assertEqual([c["manifest_index"] for c in report["clips"]], [0, 1, 2])
        self.assertTrue(all(c["status"] == "processed" for c in report["clips"]))
        self.assertTrue(report["output"]["complete"])
        self.assertEqual(report["summary"], {
            "total": 3,
            "processed": 3,
            "unprocessed": 0,
            "processed_indices": [0, 1, 2],
            "unprocessed_indices": [],
        })
        self.assertEqual(report["clips"][0]["start_frame"], 0)
        self.assertEqual(report["clips"][1]["start_frame"], 4000)
        self.assertEqual(report["clips"][1]["output_offset_frame"], 4000)
        self.assertTrue(self.report.exists())

    # ---------- 要求 2：0.5s + 0.5s = 1.0s，顺序一致 ----------
    def test_req2_two_half_second_clips_make_one_second(self) -> None:
        self.make_source(frames=16000, sample_rate=8000)
        self.write_manifest([
            {"start": 0.5, "end": 1.0},
            {"start": 0.0, "end": 0.5},
        ])
        report = self.run_ok()
        params, values = read_frames(self.out)
        self.assertEqual(params["frames"], 8000)
        self.assertAlmostEqual(params["frames"] / params["sample_rate"], 1.0, delta=1 / 8000)
        self.assertEqual(report["output"]["frames"], 8000)
        self.assertAlmostEqual(report["output"]["duration_seconds"], 1.0, delta=1 / 8000)

        src_values = read_frames(self.src)[1]
        # 清单顺序：先 [0.5,1.0)，再 [0,0.5)，不得按时间排序。
        self.assertEqual(values, frame_span(src_values, 1, 4000, 8000)
                         + frame_span(src_values, 1, 0, 4000))
        self.assertNotEqual(values, src_values)

    # ---------- 要求 3：成片保留采样率/声道/位宽，全部帧可读 ----------
    def test_req3_output_format_preserved_and_fully_readable(self) -> None:
        self.make_source(frames=12000, sample_rate=44100, channels=1, sample_width=2)
        self.write_manifest([{"start": 0.0, "end": 0.1}])
        report = self.run_ok()
        src_params = self.source_params()
        out_params, values = read_frames(self.out)
        self.assertEqual(out_params["sample_rate"], src_params["sample_rate"])
        self.assertEqual(out_params["channels"], src_params["channels"])
        self.assertEqual(out_params["sample_width"], src_params["sample_width"])
        self.assertEqual(len(values), out_params["frames"] * out_params["channels"])
        self.assertEqual(out_params["frames"], 4410)
        self.assertEqual(report["verification"]["wave_readback"], "ok")
        self.assertEqual(report["verification"]["riff_structure"], "ok")
        riff = verify_riff_structure(str(self.out))
        self.assertEqual(riff["data_bytes"], out_params["frames"] * 2)
        self.assertEqual(riff["file_size"], os.path.getsize(self.out))

    # ---------- 要求 4：双声道分别裁剪，不混音 ----------
    def test_req4_stereo_channels_kept_independent(self) -> None:
        self.make_source(frames=16000, sample_rate=8000, channels=2, sample_width=2)
        self.write_manifest([{"start": 0.25, "end": 0.75}])
        self.run_ok()
        params, out_values = read_frames(self.out)
        self.assertEqual(params["channels"], 2)
        self.assertEqual(params["frames"], 4000)
        _, src_values = read_frames(self.src)

        left_out = out_values[0::2]
        right_out = out_values[1::2]
        left_src = src_values[0::2]
        right_src = src_values[1::2]
        self.assertEqual(left_out, left_src[2000:6000])
        self.assertEqual(right_out, right_src[2000:6000])
        # 左右声道样本本身不同，确认没有把单声道数据复制到两边。
        self.assertNotEqual(left_out, right_out)

    def test_req4_stereo_gain_applies_to_both_channels_independently(self) -> None:
        self.make_source(frames=4000, sample_rate=8000, channels=2, sample_width=2)
        self.write_manifest([{"start": 0.0, "end": 0.25, "gain": 0.5}])
        self.run_ok()
        _, out_values = read_frames(self.out)
        _, src_values = read_frames(self.src)
        expected = [expected_gain_sample(v, 0.5, 2) for v in src_values[:4000]]
        self.assertEqual(out_values, expected)

    # ---------- 要求 5：gain 缺省/越界 ----------
    def test_req5_default_gain_equals_explicit_one(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([
            {"start": 0.0, "end": 0.5},
            {"start": 0.5, "end": 1.0, "gain": 1.0},
        ])
        report = self.run_ok()
        self.assertEqual([c["gain"] for c in report["clips"]], [1.0, 1.0])
        _, out_values = read_frames(self.out)
        _, src_values = read_frames(self.src)
        self.assertEqual(out_values, src_values)

    def test_req5_gain_half_scales_samples(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([{"start": 0.0, "end": 0.5, "gain": 0.5}])
        self.run_ok()
        _, out_values = read_frames(self.out)
        _, src_values = read_frames(self.src)
        self.assertEqual(out_values,
                         [expected_gain_sample(v, 0.5, 2) for v in src_values[:4000]])

    def test_req5_gain_out_of_range_rejected(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([
            {"start": 0.0, "end": 0.5, "gain": 5.0},
            {"start": 0.0, "end": 0.5, "gain": -0.1},
            {"start": 0.0, "end": 0.5, "gain": 4.01},
            {"start": 0.0, "end": 0.5, "gain": "loud"},
            {"start": 0.0, "end": 0.5, "gain": None},
        ])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(self.out.exists())
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["unprocessed_indices"], [0, 1, 2, 3, 4])
        for clip in report["clips"]:
            self.assertEqual(clip["status"], "unprocessed")
            self.assertEqual(clip["reason"], "invalid_gain")

    def test_req5_gain_boundaries_accepted(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([
            {"start": 0.0, "end": 0.25, "gain": 0.0},
            {"start": 0.25, "end": 0.5, "gain": 4.0},
        ])
        report = self.run_ok()
        self.assertEqual(report["summary"]["processed"], 2)
        _, out_values = read_frames(self.out)
        self.assertTrue(all(v == 0 for v in out_values[:2000]))
        self.assertEqual(out_values[2000:],
                         [expected_gain_sample(v, 4.0, 2)
                          for v in read_frames(self.src)[1][2000:4000]])

    def test_req5_gain_saturates_instead_of_wrapping(self) -> None:
        values = [32000, -32000, 1000, -1000] * 500
        from tests.helpers import write_wav_from_values
        write_wav_from_values(self.src, 8000, 1, 2, values)
        self.write_manifest([{"start": 0.0, "end": 0.25, "gain": 4.0}])
        self.run_ok()
        _, out_values = read_frames(self.out)
        self.assertEqual(out_values[0], 32767)
        self.assertEqual(out_values[1], -32768)
        self.assertEqual(out_values[2], 4000)
        self.assertEqual(out_values[3], -4000)

    # ---------- 要求 6：越界 / start>=end，逐项原因 ----------
    def test_req6_out_of_bounds_item_skipped_with_reason(self) -> None:
        self.make_source(frames=8000, sample_rate=8000)  # 1.0 秒
        self.write_manifest([
            {"start": 0.0, "end": 0.5},
            {"start": 0.75, "end": 1.25},
            {"start": 5.0, "end": 6.0},
        ])
        report = self.run_ok()
        self.assertEqual(report["summary"]["processed_indices"], [0])
        self.assertEqual(report["summary"]["unprocessed_indices"], [1, 2])
        self.assertFalse((self.dir / "unused").exists())
        self.assertEqual(report["clips"][1]["reason"], "out_of_bounds")
        self.assertIn("超出源时长", report["clips"][1]["reasons"][0]["message"])
        self.assertEqual(report["clips"][2]["reason"], "out_of_bounds")
        _, out_values = read_frames(self.out)
        self.assertEqual(out_values, read_frames(self.src)[1][:4000])

    def test_req6_inverted_and_negative_ranges_rejected(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([
            {"start": 0.5, "end": 0.5},
            {"start": 0.8, "end": 0.2},
            {"start": -0.1, "end": 0.5},
        ])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.out.exists())
        report = self.read_report()
        self.assertEqual([c["reason"] for c in report["clips"]],
                         ["start_after_end", "start_after_end", "negative_start"])

    # ---------- 要求 7：源缺失 / 非 PCM ----------
    def test_req7_missing_source_nonzero_exit_and_no_output(self) -> None:
        self.src = self.dir / "does-not-exist.wav"
        self.write_manifest([{"start": 0.0, "end": 0.5}])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.out.exists())
        report = self.read_report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["errors"][0]["code"], "source_missing")
        self.assertFalse(report["output"]["created"])
        self.assertEqual(report["summary"]["processed"], 0)

    def test_req7_non_pcm_source_rejected(self) -> None:
        write_non_pcm_wav(self.src)
        self.write_manifest([{"start": 0.0, "end": 0.005}])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.out.exists())
        report = self.read_report()
        self.assertEqual(report["errors"][0]["code"], "source_not_pcm")

    def test_req7_garbage_file_rejected(self) -> None:
        self.src.write_bytes(b"not a wav at all")
        self.write_manifest([{"start": 0.0, "end": 0.1}])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.read_report()["errors"][0]["code"], "source_not_pcm")

    # ---------- 要求 8：合法/非法混合，成片等于仅合法拼接 ----------
    def test_req8_output_equals_valid_only_concatenation(self) -> None:
        self.make_source(frames=24000, sample_rate=8000, channels=2, sample_width=2)
        mixed = [
            {"start": 0.0, "end": 0.5},                          # valid
            {"start": 2.0, "end": 2.5, "gain": 9.0},             # invalid gain
            {"start": 1.0, "end": 1.5, "gain": 2.0},             # valid
            {"start": 7.0, "end": 8.0},                          # out of bounds
            {"start": 1.5, "end": 1.25},                         # inverted
            {"start": 2.5, "end": 3.0},                          # valid
        ]
        self.write_manifest(mixed)
        report = self.run_ok()
        self.assertEqual(report["summary"]["processed_indices"], [0, 2, 5])
        self.assertEqual(report["summary"]["unprocessed_indices"], [1, 3, 4])
        self.assertEqual(
            [c["reason"] for c in report["clips"] if c["status"] == "unprocessed"],
            ["invalid_gain", "out_of_bounds", "start_after_end"],
        )
        self.assertFalse(report["output"]["complete"])

        _, src_values = read_frames(self.src)
        expected = (
            frame_span(src_values, 2, 0, 4000)
            + [expected_gain_sample(v, 2.0, 2)
               for v in frame_span(src_values, 2, 8000, 12000)]
            + frame_span(src_values, 2, 20000, 24000)
        )
        _, mixed_out = read_frames(self.out)

        # 对照组：只含合法项，应逐字节一致。
        only_valid = self.dir / "only-valid.json"
        only_valid.write_text(json.dumps([mixed[i] for i in (0, 2, 5)]), encoding="utf-8")
        clean_out = self.dir / "clean.wav"
        clean_report = self.dir / "clean-report.json"
        clean_result = run(str(self.src), str(only_valid), str(clean_out), str(clean_report))
        self.assertEqual(clean_result.exit_code, 0)
        self.assertEqual(mixed_out, read_frames(clean_out)[1])
        self.assertEqual(mixed_out, expected)
        self.assertEqual(read_frames(clean_out)[0]["frames"], 12000)
        self.assertEqual(self.out.read_bytes(), clean_out.read_bytes())

    # ---------- 其它边界 ----------
    def test_empty_manifest_fails_without_output(self) -> None:
        self.make_source()
        self.write_manifest([])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.read_report()["errors"][0]["code"], "no_valid_clips")

    def test_invalid_manifest_json(self) -> None:
        self.make_source()
        self.manifest.write_text("{oops", encoding="utf-8")
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.read_report()["errors"][0]["code"], "manifest_invalid_json")

    def test_manifest_must_be_array(self) -> None:
        self.make_source()
        self.manifest.write_text('{"clips": []}', encoding="utf-8")
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.read_report()["errors"][0]["code"],
                         "manifest_invalid_structure")

    def test_bad_entries_reported_individually(self) -> None:
        self.make_source()
        self.write_manifest([
            "nope",
            {"start": 0.0},
            {"start": "a", "end": 1.0},
            {"start": True, "end": 1.0},
            {"start": 0.0, "end": 0.5},
        ])
        report = self.run_ok()
        reasons = [c["reason"] for c in report["clips"][:4]]
        self.assertEqual(reasons,
                         ["invalid_entry", "invalid_entry", "invalid_entry", "invalid_entry"])
        self.assertEqual(report["summary"]["processed_indices"], [4])

    def test_overlapping_clips_sum_durations(self) -> None:
        self.make_source(frames=8000, sample_rate=8000)
        self.write_manifest([
            {"start": 0.0, "end": 0.75},
            {"start": 0.5, "end": 1.0},
        ])
        report = self.run_ok()
        # 区间重叠不影响时长：两段各自完整保留后顺序拼接。
        self.assertEqual(report["output"]["frames"], 10000)
        self.assertEqual(report["clips"][1]["output_offset_frame"], 6000)
        _, src_values = read_frames(self.src)
        self.assertEqual(read_frames(self.out)[1],
                         src_values[:6000] + src_values[4000:8000])

    def test_output_never_overwrites_source(self) -> None:
        self.make_source()
        self.write_manifest([{"start": 0.0, "end": 0.5}])
        before = self.src.read_bytes()
        result = run(str(self.src), str(self.manifest), str(self.src), str(self.report))
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(self.read_report()["errors"][0]["code"],
                         "output_conflicts_with_source")

    def test_sample_widths_roundtrip(self) -> None:
        for width in (1, 2, 3, 4):
            with self.subTest(sample_width=width):
                self.make_source(frames=8000, sample_width=width)
                self.write_manifest([{"start": 0.0, "end": 0.5, "gain": 0.5}])
                result = run(str(self.src), str(self.manifest), str(self.out),
                             str(self.report))
                self.assertEqual(result.exit_code, 0, result.report.get("errors"))
                params, out_values = read_frames(self.out)
                self.assertEqual(params["sample_width"], width)
                _, src_values = read_frames(self.src)
                expected = [expected_gain_sample(v, 0.5, width)
                            for v in src_values[:4000]]
                self.assertEqual(out_values, expected)

    def test_report_orders_clips_by_manifest(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([
            {"start": 0.5, "end": 1.0},
            {"start": 2.0, "end": 3.0},
            {"start": 0.0, "end": 0.5},
        ])
        report = self.run_ok()
        self.assertEqual([c["manifest_index"] for c in report["clips"]], [0, 1, 2])
        self.assertEqual([c["status"] for c in report["clips"]],
                         ["processed", "unprocessed", "processed"])

    def test_cli_exit_codes_and_report(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([{"start": 0.0, "end": 0.5}])
        proc = subprocess.run(
            [sys.executable, "-m", "clipforge",
             "--source", str(self.src), "--manifest", str(self.manifest),
             "--output", str(self.out), "--report", str(self.report)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.out.exists())
        self.assertIn("成片已生成", proc.stdout)

        proc = subprocess.run(
            [sys.executable, "-m", "clipforge",
             "--source", str(self.dir / "missing.wav"), "--manifest", str(self.manifest),
             "--output", str(self.out), "--report", str(self.report)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("source_missing", proc.stdout + proc.stderr)

    def test_truncated_source_data_reported(self) -> None:
        self.make_source(frames=8000)
        data = self.src.read_bytes()
        # 去掉 data 块后半段，但不改头部声明。
        self.src.write_bytes(data[:-2000])
        self.write_manifest([{"start": 0.0, "end": 1.0}])
        result = run(str(self.src), str(self.manifest), str(self.out), str(self.report))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.out.exists())
        self.assertIn(self.read_report()["errors"][0]["code"],
                      {"source_truncated", "source_read_error"})
        # 源故障时合法清单项也应出现在报告中，并标记为 run_failed。
        self.assertEqual(self.read_report()["clips"][0]["reason"], "run_failed")

    def test_streaming_chunk_boundaries_do_not_affect_output(self) -> None:
        """长片段按块流式处理，块边界不得改变任何样本。"""
        from clipforge import wavio
        self.make_source(frames=140000, sample_rate=8000)  # 17.5 秒，跨多个读取块
        self.write_manifest([{"start": 0.0, "end": 17.5, "gain": 1.5}])
        report = self.run_ok()
        self.assertEqual(report["output"]["frames"], 140000)
        _, out_values = read_frames(self.out)
        _, src_values = read_frames(self.src)
        self.assertEqual(out_values,
                         [expected_gain_sample(v, 1.5, 2) for v in src_values])

        monolithic = wavio.apply_gain_bytes(encode(src_values, 2), 2, 1.5)
        self.assertEqual(decode(monolithic, 2), out_values)

    def test_report_is_deterministic(self) -> None:
        self.make_source(frames=8000)
        self.write_manifest([{"start": 0.0, "end": 0.5}])
        self.run_ok()
        first = self.read_report()
        out_bytes = self.out.read_bytes()
        self.run_ok()
        second = self.read_report()
        self.assertEqual(first, second)
        self.assertEqual(out_bytes, self.out.read_bytes())
        self.assertEqual(first["output"]["sha256"], second["output"]["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
