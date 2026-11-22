from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from motion_pipeline.config import PipelineConfig
from motion_pipeline.dataset import generate_dataset, load_dataset
from motion_pipeline.errors import BusinessError
from motion_pipeline.pipeline import Pipeline
from motion_pipeline.snapshots import SnapshotStore

from tests.helpers import DatasetTestCase


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ResourceTests(DatasetTestCase):
    def test_frame_count_resource_limit(self) -> None:
        self.assert_business_error(
            "E_RESOURCE_LIMIT",
            lambda: load_dataset(
                self.manifest,
                self.frames,
                PipelineConfig(max_frames_per_clip=1),
            ),
            field="frame_count",
            has_clip_id=True,
        )

    def test_resident_history_error(self) -> None:
        workdir = self.root / "resident-error"
        with self.assertRaises(BusinessError) as caught:
            self.run_pipeline(
                workdir,
                PipelineConfig(
                    max_resident_history_windows=1,
                    resource_policy="error",
                ),
            )
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertIsNotNone(caught.exception.clip_id)
        checkpoint = SnapshotStore(workdir).load()
        self.assertIsNone(checkpoint)

    def test_degradation_does_not_change_committed_artifacts(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        degraded = self.run_pipeline(
            self.root / "degraded",
            PipelineConfig(
                max_resident_history_windows=1,
                max_parallel_clips=1,
                resource_policy="degrade",
            ),
        )
        self.assertEqual(
            baseline.final_result_bytes, degraded.final_result_bytes
        )
        self.assertEqual(
            (
                self.root / "baseline" / "snapshots" / "checkpoint.json"
            ).read_bytes(),
            (
                self.root / "degraded" / "snapshots" / "checkpoint.json"
            ).read_bytes(),
        )
        self.assertTrue(degraded.report["degradations"])
        categories = {item["category"] for item in degraded.report["degradations"]}
        self.assertIn("history_window_batched", categories)
        self.assertIn("parallel_sequential", categories)

    def test_parallel_degradation_with_more_clips(self) -> None:
        dataset = self.root / "many"
        generate_dataset(dataset, seed=77, clip_count=6)
        limited = Pipeline(
            self.root / "limited", PipelineConfig(max_parallel_clips=2)
        ).run(dataset / "manifest.jsonl", dataset / "frames.jsonl")
        wide = Pipeline(
            self.root / "wide", PipelineConfig(max_parallel_clips=8)
        ).run(dataset / "manifest.jsonl", dataset / "frames.jsonl")
        self.assertEqual(limited.final_result_bytes, wide.final_result_bytes)
        self.assertEqual(
            limited.report["degradations"][0]["category"],
            "parallel_sequential",
        )


class CliIntegrationTests(DatasetTestCase):
    clips = 2

    def test_cli_kill_then_resume_is_byte_identical(self) -> None:
        baseline_dir = self.root / "baseline"
        crashed_dir = self.root / "crashed"
        common = [
            sys.executable,
            "-m",
            "motion_pipeline",
            "run",
            "--manifest",
            str(self.manifest),
            "--frames",
            str(self.frames),
        ]
        baseline = subprocess.run(
            [
                *common,
                "--workdir",
                str(baseline_dir),
                "--reset",
                "--report",
                str(baseline_dir / "reports" / "run.json"),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(baseline.returncode, 0)
        killed_report = crashed_dir / "reports" / "killed.json"
        killed = subprocess.run(
            [
                *common,
                "--workdir",
                str(crashed_dir),
                "--reset",
                "--kill-after-stage",
                "2",
                "--report",
                str(killed_report),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(killed.returncode, 137)
        self.assertFalse(killed_report.exists())
        resumed = subprocess.run(
            [
                *common,
                "--workdir",
                str(crashed_dir),
                "--report",
                str(crashed_dir / "reports" / "run.json"),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(resumed.returncode, 0)
        self.assertEqual(
            (baseline_dir / "final" / "result.json").read_bytes(),
            (crashed_dir / "final" / "result.json").read_bytes(),
        )
        self.assertEqual(
            (baseline_dir / "snapshots" / "checkpoint.json").read_bytes(),
            (crashed_dir / "snapshots" / "checkpoint.json").read_bytes(),
        )

    def test_sigterm_exits_at_commit_boundary_with_report(self) -> None:
        large = self.root / "large"
        generate_dataset(large, seed=808, clip_count=18)
        workdir = self.root / "signal"
        report_path = workdir / "reports" / "exit.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "motion_pipeline",
                "run",
                "--manifest",
                str(large / "manifest.jsonl"),
                "--frames",
                str(large / "frames.jsonl"),
                "--workdir",
                str(workdir),
                "--reset",
                "--report",
                str(report_path),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        checkpoint_path = workdir / "snapshots" / "checkpoint.json"
        deadline = time.monotonic() + 10.0
        while not checkpoint_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                self.fail("pipeline did not initialize in time")
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130, msg=stderr)
        self.assertTrue(report_path.exists(), msg=f"stdout={stdout} stderr={stderr}")
        report = json.loads(report_path.read_text())
        self.assertTrue(report["ok"])
        checkpoint = SnapshotStore(workdir).load()
        self.assertIsNotNone(checkpoint)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["signal"], signal.SIGTERM)
