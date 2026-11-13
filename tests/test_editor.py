"""单元测试：不依赖子进程，直接覆盖引擎核心语义与错误码。

运行::

    python -I -m unittest -v tests.test_editor
"""

from __future__ import annotations

import array
import json
import os
import shutil
import sys
import tempfile
import unittest
import wave

# 隔离模式（python -I）下直接运行本文件时，确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from podcast_editor import EditorEngine, EditError, ErrorCode, build_report, load_wav
from scripts.generate_samples import SR, generate


class EditorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="podcast_editor_test_")
        self.input_dir = os.path.join(self.tmp, "input")
        self.work_dir = os.path.join(self.tmp, "work")
        self.out_wav = os.path.join(self.tmp, "out.wav")
        self.report_path = os.path.join(self.tmp, "report.json")
        generate(self.input_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_engine(self, instructions: list, fresh: bool = False) -> dict:
        engine = EditorEngine(
            instructions=instructions,
            base_dir=self.input_dir,
            work_dir=self.work_dir,
            output_wav=self.out_wav,
            fresh=fresh,
        )
        engine.run()
        report = build_report(
            engine,
            instruction_file=os.path.join(self.tmp, "edits.json"),
            output_wav=self.out_wav,
            resumed=getattr(engine, "resumed", False),
        )
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False)
        return report

    def read_output(self):
        with wave.open(self.out_wav, "rb") as wf:
            params = wf.getparams()
            samples = array.array("h")
            samples.frombytes(wf.readframes(wf.getnframes()))
        return params, samples


class TestValidation(EditorTestBase):
    def test_stereo_source_rejected(self):
        report = self.run_engine([
            {"type": "crop", "source": "bg_stereo.wav",
             "params": {"start": 0, "end": 1}},
        ])
        self.assertEqual(report["overall_status"], "failed")
        err = report["instructions"][0]["error"]
        self.assertEqual(err["code"], ErrorCode.INVALID_WAV_FORMAT)
        self.assertIn("单声道", err["message"])
        self.assertFalse(os.path.exists(self.out_wav))

    def test_missing_source_retryable(self):
        report = self.run_engine([
            {"type": "crop", "source": "ghost.wav",
             "params": {"start": 0, "end": 1}},
        ])
        err = report["instructions"][0]["error"]
        self.assertEqual(err["code"], ErrorCode.SOURCE_NOT_FOUND)
        self.assertTrue(err["retryable"])

    def test_crop_time_out_of_range(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 9, "end": 12}},
        ])
        self.assertEqual(
            report["instructions"][0]["error"]["code"],
            ErrorCode.TIME_OUT_OF_RANGE,
        )

    def test_crop_start_not_before_end(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 5, "end": 5}},
        ])
        self.assertEqual(
            report["instructions"][0]["error"]["code"],
            ErrorCode.TIME_OUT_OF_RANGE,
        )

    def test_insert_point_out_of_range(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 0, "end": 2}},
            {"type": "splice", "source": "ad_mono.wav",
             "params": {"at": 99}},
        ])
        self.assertEqual(report["instructions"][1]["status"], "failed")
        self.assertEqual(
            report["instructions"][1]["error"]["code"],
            ErrorCode.INSERT_OUT_OF_RANGE,
        )
        # 失败无副作用：仍是 2s
        self.assertEqual(report["summary"]["total_frames"], 2 * SR)

    def test_gain_boundaries(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 0, "end": 1}},
            {"type": "volume", "source": None, "params": {"gain_db": 60}},
            {"type": "volume", "source": None, "params": {"gain_db": -60}},
            {"type": "volume", "source": None, "params": {"gain_db": 60.1}},
            {"type": "volume", "source": None, "params": {"gain_db": -60.1}},
        ])
        statuses = [i["status"] for i in report["instructions"]]
        self.assertEqual(statuses, ["success", "success", "success",
                                    "failed", "failed"])
        for idx in (3, 4):
            self.assertEqual(
                report["instructions"][idx]["error"]["code"],
                ErrorCode.GAIN_OUT_OF_RANGE,
            )

    def test_unknown_instruction_type(self):
        report = self.run_engine([
            {"type": "reverse", "source": "voice_mono.wav", "params": {}},
        ])
        self.assertEqual(
            report["instructions"][0]["error"]["code"],
            ErrorCode.UNKNOWN_INSTRUCTION_TYPE,
        )


class TestTimelineSemantics(EditorTestBase):
    def test_crop_splice_volume_sequence(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 1, "end": 4}},          # 3s
            {"type": "splice", "source": "ad_mono.wav",
             "params": {"at": 3}},                       # +3s = 6s
            {"type": "volume", "source": None,
             "params": {"gain_db": -6}},                 # 6s 不变
        ])
        self.assertEqual(report["overall_status"], "complete")
        self.assertEqual(report["summary"]["total_frames"], 6 * SR)
        params, samples = self.read_output()
        self.assertEqual((params.nchannels, params.sampwidth,
                          params.framerate), (1, 2, SR))
        self.assertTrue(all(-32768 <= v <= 32767 for v in samples))

    def test_partial_failure_keeps_successful_output(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 0, "end": 2}},
            {"type": "crop", "source": "missing.wav",
             "params": {"start": 0, "end": 1}},
            {"type": "splice", "source": "ad_mono.wav",
             "params": {"at": 2}},                       # 2+3 = 5s
        ])
        self.assertEqual(report["overall_status"], "incomplete")
        self.assertEqual(report["summary"]["total_frames"], 5 * SR)
        self.assertTrue(os.path.exists(self.out_wav))
        unavailable = {s["source"] for s in report["unavailable_sources"]}
        self.assertIn("missing.wav", unavailable)

    def test_clipping_prevention(self):
        report = self.run_engine([
            {"type": "crop", "source": "jingle_mono.wav",
             "params": {"start": 0, "end": 1}},
            {"type": "volume", "source": None,
             "params": {"gain_db": 60}},
        ])
        self.assertEqual(report["overall_status"], "complete")
        _, samples = self.read_output()
        self.assertLessEqual(max(samples), 32767)
        self.assertGreaterEqual(min(samples), -32768)
        # 系数 1000 下任何非零样本都触顶：至少一个样本贴到对称钳位边界
        self.assertTrue(
            any(abs(v) in (32767, 32768) for v in samples),
            "未观察到钳位样本",
        )

    def test_volume_segment_scope(self):
        report = self.run_engine([
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 0, "end": 4}},
            {"type": "volume", "source": None,
             "params": {"gain_db": -60, "start": 0, "end": 2}},
        ])
        _, samples = self.read_output()
        # 前 2s 被 -60dB（系数 0.001）压到接近静音：原幅度约 12000 -> ≤12
        self.assertTrue(all(abs(v) <= 12 for v in samples[: 2 * SR]))
        # 后 2s 未处理，保持原始幅度
        self.assertTrue(any(abs(v) > 1000 for v in samples[2 * SR:]))


class TestResume(EditorTestBase):
    def test_retry_after_transient_failure(self):
        instructions = [
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 1, "end": 3}},             # 2s
            {"type": "splice", "source": "ad_mono.wav",
             "params": {"at": 2}},                          # +3s = 5s
        ]
        ad_path = os.path.join(self.input_dir, "ad_mono.wav")
        hidden = ad_path + ".hidden"

        os.replace(ad_path, hidden)
        try:
            report1 = self.run_engine(instructions, fresh=True)
        finally:
            os.replace(hidden, ad_path)
        self.assertEqual(report1["instructions"][1]["status"], "failed")
        self.assertEqual(report1["summary"]["total_frames"], 2 * SR)
        params1, samples1 = self.read_output()

        report2 = self.run_engine(instructions, fresh=False)
        self.assertTrue(report2["resumed_from_checkpoint"])
        self.assertEqual(report2["overall_status"], "complete")
        self.assertEqual(report2["summary"]["total_frames"], 5 * SR)
        self.assertTrue(report2["instructions"][0]["resumed_skipped"])
        self.assertFalse(report2["instructions"][1]["resumed_skipped"])

        params2, samples2 = self.read_output()
        # 已成功部分逐样本不变
        self.assertEqual(samples2[: 2 * SR], samples1)
        self.assertEqual(len(samples2), 5 * SR)

    def test_fresh_ignores_checkpoint(self):
        instructions = [
            {"type": "crop", "source": "voice_mono.wav",
             "params": {"start": 0, "end": 1}},
        ]
        report1 = self.run_engine(instructions, fresh=True)
        report2 = self.run_engine(instructions, fresh=True)
        self.assertFalse(report2["resumed_from_checkpoint"])
        self.assertEqual(report2["summary"]["total_frames"], SR)


class TestWavIO(unittest.TestCase):
    def test_load_wav_returns_mono_pcm(self):
        tmp = tempfile.mkdtemp(prefix="podcast_wav_")
        try:
            paths = generate(tmp)
            data = load_wav(paths["voice_mono"])
            self.assertEqual(data.sample_rate, SR)
            self.assertEqual(data.frame_count, 10 * SR)
            with self.assertRaises(EditError) as ctx:
                load_wav(paths["bg_stereo"])
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_WAV_FORMAT)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
