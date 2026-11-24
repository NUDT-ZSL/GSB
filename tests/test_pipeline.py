from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_pipeline.errors import PipelineError
from audio_pipeline.pipeline import PipelineOptions, run_pipeline
from audio_pipeline.wavio import inspect_wav

from tests.helpers import configure_compression, make_workspace, read_json, write_json


class PipelineTests(unittest.TestCase):
    def _run(self, workspace: Path, **kwargs):
        return run_pipeline(
            workspace / "task.json",
            PipelineOptions(workspace_root=workspace, **kwargs),
        )

    def test_full_delivery_order_budgets_anomalies_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            before_revision = None
            outcome = self._run(workspace)
            self.assertEqual(outcome.exit_code, 3)
            self.assertEqual(outcome.report["status"], "degraded")
            report = read_json(workspace / "output" / "report.json")
            before_revision = report["criteria"]["snapshot_summary"][
                "measurement_revision"
            ]

            inputs = report["inputs"]
            self.assertEqual(
                [item["input_id"] for item in inputs],
                ["promo", "voice", "bad_depth", "silent", "wrong_rate"],
            )
            self.assertEqual([item["order"] for item in inputs], [0, 1, 2, 3, 4])
            by_id = {item["input_id"]: item for item in inputs}
            self.assertEqual(by_id["bad_depth"]["business_codes"], ["E_BIT_DEPTH"])
            self.assertEqual(by_id["wrong_rate"]["business_codes"], ["E_SR"])
            self.assertEqual(by_id["silent"]["business_codes"], ["E_SILENT"])
            self.assertIsNone(by_id["silent"]["measurement"]["integrated_lufs"])
            self.assertTrue(by_id["silent"]["measurement"]["silent"])
            self.assertEqual(by_id["silent"]["decision"]["action"], "none")

            self.assertEqual(report["summary"]["measured_inputs"], 3)
            self.assertEqual(report["summary"]["rejected_inputs"], 2)
            self.assertEqual(report["summary"]["anomalies_detected"], 3)
            self.assertGreaterEqual(report["summary"]["criteria_conflicts"], 6)
            scheduler = report["scheduler"]
            self.assertLessEqual(
                scheduler["peak_decode_bytes"], scheduler["decode_budget_bytes"]
            )
            self.assertLessEqual(
                scheduler["peak_sample_bytes"], scheduler["sample_budget_bytes"]
            )
            anomalies = {
                item["injection_id"]: item for item in report["anomaly_injections"]
            }
            self.assertTrue(all(
                item["status"] == "detected" for item in anomalies.values()
            ))
            self.assertEqual(
                anomalies["inject-corrupt-shard"]["locations"][0]["shard_id"],
                "aux",
            )

            for input_id, expected_frames in {
                "promo": 124800,
                "voice": 96000,
                "silent": 57600,
            }.items():
                path = workspace / "output" / f"{input_id}.wav"
                info = inspect_wav(path)
                self.assertEqual(info.sample_rate, 48000)
                self.assertEqual(info.bits_per_sample, 24)
                self.assertEqual(info.container_bits, 24)
                self.assertEqual(info.frames, expected_frames)
                theoretical = by_id[input_id]["output"]["theoretical_frame_count"]
                self.assertLessEqual(abs(info.frames - theoretical), 1)

            # A second identical run reuses persisted chunks and skips replacement.
            first_report_bytes = (workspace / "output" / "report.json").read_bytes()
            second = self._run(workspace)
            self.assertEqual(second.exit_code, 3)
            connection = sqlite3.connect(workspace / "state" / "state.sqlite3")
            try:
                metadata = json.loads(connection.execute(
                    "SELECT metadata_json FROM runs ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0])
            finally:
                connection.close()
            self.assertTrue(metadata["commit"]["skipped"])
            self.assertEqual(
                first_report_bytes,
                (workspace / "output" / "report.json").read_bytes(),
            )
            self.assertIsNotNone(before_revision)

    def test_decision_only_change_reuses_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            first = self._run(workspace)
            self.assertEqual(first.exit_code, 3)
            report_before = read_json(workspace / "output" / "report.json")
            measurement_before = report_before["criteria"]["snapshot_summary"][
                "measurement_revision"
            ]
            decision_before = report_before["criteria"]["snapshot_summary"][
                "decision_revision"
            ]

            configure_compression(
                workspace, threshold_lufs=-2.0, ratio=1.0, enabled=False
            )
            second = self._run(workspace, resume=True)
            self.assertEqual(second.exit_code, 3)
            report = read_json(workspace / "output" / "report.json")
            summary = report["criteria"]["snapshot_summary"]
            self.assertEqual(summary["measurement_revision"], measurement_before)
            self.assertNotEqual(summary["decision_revision"], decision_before)
            self.assertEqual(report["summary"]["measurement_reused_inputs"], 3)
            promo = next(item for item in report["inputs"] if item["input_id"] == "promo")
            self.assertTrue(promo["measurement"]["measurement_reused"])
            self.assertFalse(promo["decision"]["limiter"])
            self.assertEqual(promo["decision"]["action"], "gain_down")

    def test_input_config_error_does_not_abort_other_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            task_path = workspace / "task.json"
            task = read_json(task_path)
            task["inputs"].insert(1, {
                "id": "bad_weights",
                "canonical_id": "program/bad-weights",
                "path": "media/promo.wav",
                "layout": "stereo",
                "channels": 2,
                "weights": [-1.0, 0.5],
                "declared_sample_rate": 48000,
                "declared_bit_depth": 24,
            })
            write_json(task_path, task)
            outcome = self._run(workspace)
            report = read_json(workspace / "output" / "report.json")
            bad = next(
                item for item in report["inputs"]
                if item["input_id"] == "bad_weights"
            )
            self.assertEqual(bad["status"], "rejected")
            self.assertIn("E_WEIGHTS", bad["business_codes"])
            self.assertEqual(report["summary"]["measured_inputs"], 3)

    def test_atomic_failure_rolls_back_audio_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            self._run(workspace)
            output = workspace / "output"
            old_files = {
                path.name: path.read_bytes()
                for path in output.iterdir() if path.is_file()
            }
            configure_compression(
                workspace, threshold_lufs=-9.0, ratio=3.0, enabled=True
            )
            os.environ["AUDIO_PIPELINE_FAIL_COMMIT"] = "report"
            try:
                outcome = self._run(workspace)
            finally:
                os.environ.pop("AUDIO_PIPELINE_FAIL_COMMIT", None)
            self.assertEqual(outcome.exit_code, 4)
            for name, data in old_files.items():
                self.assertEqual((output / name).read_bytes(), data)
            self.assertEqual(
                set(path.name for path in output.iterdir() if path.is_file()),
                set(old_files),
            )

    def test_runtime_failure_returns_first_error_without_replacing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            self._run(workspace)
            output = workspace / "output"
            old_files = {
                path.name: path.read_bytes()
                for path in output.iterdir() if path.is_file()
            }
            from audio_pipeline import measurement as measurement_module
            from audio_pipeline import pipeline as pipeline_module
            original_decode = measurement_module.decode_wav

            def selective_decode(info, block_frames=16384, on_block=None):
                if info.path.name == "voice.wav":
                    raise PipelineError("E_WAV", "injected decode failure",
                                        input_id="voice")
                return original_decode(info, block_frames=block_frames,
                                       on_block=on_block)

            with patch.object(measurement_module, "decode_wav", selective_decode), \
                    patch.object(pipeline_module, "decode_wav", selective_decode):
                outcome = self._run(workspace)
            self.assertEqual(outcome.exit_code, 4)
            self.assertTrue(outcome.interrupted is False)
            self.assertIsNotNone(outcome.report["first_error"])
            self.assertEqual(outcome.report["first_error"]["code"], "E_WAV")
            self.assertEqual(outcome.report_path.name, "partial_report.json")
            for name, data in old_files.items():
                self.assertEqual((output / name).read_bytes(), data)

    def test_target_16_bit_and_rational_resampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            task_path = workspace / "task.json"
            task = read_json(task_path)
            task["measurement"]["target_sample_rate"] = 44100
            task["measurement"]["output_bit_depth"] = 16
            write_json(task_path, task)
            outcome = self._run(workspace)
            self.assertEqual(outcome.exit_code, 3)
            expected = {"promo": 114660, "voice": 88200, "silent": 52920}
            for input_id, frames in expected.items():
                info = inspect_wav(workspace / "output" / f"{input_id}.wav")
                self.assertEqual(info.sample_rate, 44100)
                self.assertEqual(info.bits_per_sample, 16)
                self.assertEqual(info.frames, frames)


if __name__ == "__main__":
    unittest.main()
