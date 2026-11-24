from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from audiofp.pipeline import run_search
from generate_samples import _motif, _with_ambient, _write_wav
from tests.helpers import make_task


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rate = 16000
        reference = _motif(1234, rate, 4.0)
        _write_wav(self.root / "ref.wav", reference, rate)
        query = _with_ambient(
            reference[8000:40000],
            sample_rate=rate,
            lead_seconds=0.2,
            tail_seconds=0.3,
            gain=0.6,
            seed=99,
        )
        _write_wav(self.root / "query.wav", query, rate)
        self.task = make_task(
            self.root / "task.json",
            references=["ref.wav"],
            queries=["query.wav"],
            search={
                "vote_window_frames": 12,
                "min_matches": 20,
                "score_tolerance": 0.01,
                "time_offset_tolerance_samples": 256.0,
                "fixed_scale": 1_000_000,
                "relative_score_floor": 0.0,
            },
            chunk_frames=32,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_search_incremental_and_interrupt_resume(self) -> None:
        report = run_search(self.root / "task.json")
        self.assertEqual(report["summary"]["query_count"], 1)
        self.assertEqual(report["summary"]["segment_count"], 1)
        segment = report["segments"][0]
        self.assertEqual(segment["reference_file"], "ref.wav")
        self.assertLessEqual(
            abs(segment["integer_frame_offset"] * 128 - 4800), 128
        )
        self.assertEqual(segment["offset_conversion_error_samples"], 0.0)
        self.assertLessEqual(
            segment["offset_conversion_error_samples"],
            segment["time_offset_tolerance_samples"],
        )

        repeated = run_search(self.root / "task.json")
        self.assertEqual(repeated["incremental"]["affected_intervals"], [])
        self.assertEqual(
            repeated["incremental"]["fingerprint_chunks_reused"],
            repeated["summary"]["fingerprint_chunk_count"],
        )

        shutil.copy2(self.root / "query.wav", self.root / "query2.wav")
        task = dict(self.task)
        task["query_files"] = ["query.wav", "query2.wav"]
        task["report_path"] = "report_multi.json"
        task["segments_path"] = "segments_multi.json"
        (self.root / "task_multi.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        incremental = run_search(self.root / "task_multi.json")
        fresh_task = dict(task)
        fresh_task["state_dir"] = "fresh_state"
        fresh_task["report_path"] = "fresh_report.json"
        fresh_task["segments_path"] = "fresh_segments.json"
        (self.root / "task_fresh.json").write_text(
            json.dumps(fresh_task, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        fresh = run_search(self.root / "task_fresh.json")

        def segment_key(item: dict) -> tuple:
            return (
                item["query_file"],
                item["reference_file"],
                item["query_start_sample"],
                item["query_end_sample"],
                item["reference_start_sample"],
                item["reference_end_sample"],
                item["integer_frame_offset"],
                item["fixed_score_units"],
                item["match_count"],
            )

        self.assertEqual(
            sorted(map(segment_key, incremental["segments"])),
            sorted(map(segment_key, fresh["segments"])),
        )

    def test_cli_exit_code_and_resume_after_chunk_boundary_interrupt(self) -> None:
        baseline = run_search(self.root / "task.json")
        state = self.root / "state"
        shutil.rmtree(state)
        (self.root / "report.json").unlink(missing_ok=True)
        (self.root / "segments.json").unlink(missing_ok=True)
        environment = dict(os.environ)
        environment["AUDIOFP_INTERRUPT_AFTER_CHUNKS"] = "2"
        interrupted = subprocess.run(
            [
                sys.executable,
                "-m",
                "audiofp.cli",
                "run",
                "--task",
                str(self.root / "task.json"),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(interrupted.returncode, 130)
        self.assertFalse((self.root / "report.json").exists())
        marker = json.loads(
            (state / "interruption.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["exit_code"], 130)
        resumed = run_search(self.root / "task.json")
        self.assertIsNotNone(resumed["incremental"]["resumed_from_interruption"])
        self.assertFalse((state / "interruption.json").exists())
        self.assertEqual(
            [(item["query_start_sample"], item["reference_start_sample"]) for item in baseline["segments"]],
            [(item["query_start_sample"], item["reference_start_sample"]) for item in resumed["segments"]],
        )


if __name__ == "__main__":
    unittest.main()
