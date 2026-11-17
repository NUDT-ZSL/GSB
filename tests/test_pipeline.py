from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from hdr_color.errors import BusinessError
from hdr_color.processor import TerminationController, run_processing
from hdr_color.task import load_task
from tests.helpers import base_task, frame_value, write_case


def _run(task_path: Path) -> dict[str, object]:
    loaded = load_task(task_path)
    return run_processing(
        loaded.config,
        TerminationController(event=threading.Event()),
    )


class PipelineTests(unittest.TestCase):
    def test_full_pipeline_and_statistics_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index, pixels=6) for index in range(8)]
            frames[0] = [[0.0, 0.0, 0.0] for _ in range(6)]
            frames[1] = [[1.0, 1.0, 1.0] for _ in range(6)]
            frames[2] = [[4.0, 0.0, 0.0] for _ in range(6)]
            frames[3] = [[40.0, 35.0, 30.0] for _ in range(6)]
            task_path = write_case(root, base_task("out"), frames)
            outcome = _run(task_path)
            self.assertEqual(outcome["status"], "complete")
            report = json.loads(
                (root / "out/statistics_report.json").read_text(encoding="utf-8")
            )
            output = json.loads(
                (root / "out/converted_frames.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["statistics_source"], "final converted target linear-light frames only")
            self.assertEqual(output["frames_count"], 8)
            self.assertEqual(report["statistics"]["overall"]["valid_frame_count"], 8)
            black = report["statistics"]["frames"][0]
            self.assertEqual(black["luminance_mean_nits"], 0.0)
            self.assertEqual(black["luminance_peak_nits"], 0.0)
            self.assertEqual(black["over_target_peak_pixel_ratio"], 0.0)
            super_peak = report["statistics"]["frames"][3]
            self.assertLessEqual(super_peak["luminance_peak_nits"], 100.0)
            self.assertEqual(super_peak["over_target_peak_pixel_ratio"], 0.0)
            self.assertGreater(super_peak["channel_clipped_pixel_ratio"], 0.0)

            coefficients = report["curves_and_matrices"]["colour"]["target_gamut"][
                "luma_coefficients"
            ]
            recomputed = [
                sum(
                    coefficient * channel
                    for coefficient, channel in zip(coefficients, pixel)
                )
                for pixel in output["frames"][3]["linear_nits"]
            ]
            self.assertAlmostEqual(
                sum(recomputed) / len(recomputed),
                super_peak["luminance_mean_nits"],
                places=12,
            )
            self.assertAlmostEqual(
                max(recomputed),
                super_peak["luminance_peak_nits"],
                places=12,
            )

            windows = report["statistics"]["windows"]
            self.assertEqual(
                [(item["start_frame"], item["end_frame_exclusive"]) for item in windows],
                [(0, 3), (3, 6), (6, 8)],
            )
            self.assertTrue(windows[-1]["is_partial_tail"])
            self.assertEqual(windows[-1]["frame_count"], 2)

    def test_injections_continue_and_report_first_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index, pixels=4) for index in range(10)]
            task = base_task("injected")
            task["injections"] = [
                {"frame_index": 2, "kind": "NAN", "channel": 1},
                {"frame_index": 4, "kind": "NEGATIVE", "channel": 0},
                {"frame_index": 6, "kind": "PEAK_OVERFLOW"},
                {"frame_index": 8, "kind": "BAD_TRANSFER"},
            ]
            task_path = write_case(root, task, frames)
            source_hash = hashlib.sha256((root / "frames.json").read_bytes()).hexdigest()
            outcome = _run(task_path)
            self.assertEqual(outcome["status"], "complete")
            self.assertEqual(outcome["valid_frames"], 6)
            self.assertEqual(outcome["invalid_frames"], 4)
            self.assertEqual(outcome["first_injection_error_frame"], 2)
            self.assertEqual(
                hashlib.sha256((root / "frames.json").read_bytes()).hexdigest(),
                source_hash,
            )
            report = json.loads(
                (root / "injected/statistics_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["injections"]["error_frame_indices"], [2, 4, 6, 8]
            )
            self.assertEqual(report["injections"]["first_error_frame"], 2)
            codes = {
                record["frame_index"]: record["error_code"]
                for record in report["injections"]["records"]
            }
            self.assertEqual(codes[2], "E_INJECT_NAN")
            self.assertEqual(codes[4], "E_INJECT_NEGATIVE")
            self.assertEqual(codes[6], "E_INJECT_PEAK_OVERFLOW")
            self.assertEqual(codes[8], "E_INJECT_BAD_TRANSFER")
            invalid_stats = [
                item for item in report["statistics"]["frames"] if not item["valid"]
            ]
            self.assertEqual(
                [item["frame_index"] for item in invalid_stats], [2, 4, 6, 8]
            )
            first_window = report["statistics"]["windows"][0]
            self.assertEqual(first_window["valid_frame_count"], 2)
            self.assertEqual(first_window["invalid_frame_count"], 1)

    def test_chunk_failure_cancels_and_leaves_no_complete_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index, pixels=3) for index in range(8)]
            frames[5][0][0] = 1e308
            task = base_task("failed")
            task["processing"]["chunk_frames"] = 2
            task["processing"]["concurrency"] = 2
            task_path = write_case(root, task, frames)
            with self.assertRaises(BusinessError) as caught:
                _run(task_path)
            self.assertIn(
                caught.exception.code,
                {"E_FRAME_PROCESSING", "E_CHUNK_FAILURE"},
            )
            self.assertEqual(caught.exception.details["first_failed_chunk_index"], 2)
            state = json.loads(
                (root / "failed/run-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "failed")
            self.assertFalse((root / "failed/converted_frames.json").exists())
            self.assertFalse((root / "failed/statistics_report.json").exists())

    def test_memory_budget_below_one_frame_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("small-budget")
            task["processing"]["memory_budget_bytes"] = 1024
            task_path = write_case(
                root, task, [frame_value(index) for index in range(4)]
            )
            with self.assertRaises(BusinessError) as caught:
                _run(task_path)
            self.assertEqual(caught.exception.code, "E_MEMORY_BUDGET")
            self.assertFalse((root / "small-budget/converted_frames.json").exists())


if __name__ == "__main__":
    unittest.main()
