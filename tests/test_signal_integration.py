from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from regularization_lab.config import load_experiment
from regularization_lab.lab import ExperimentSession

from tests.helpers import ROOT, copy_sample_config_with_overrides, find_latest_snapshot_round, result_core


def wait_for_round(state_dir: Path, minimum_round: int, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = find_latest_snapshot_round(state_dir)
        if current >= minimum_round:
            return current
        time.sleep(0.01)
    raise AssertionError(f"snapshot round {minimum_round} was not reached in time")


def cli_command(config_path: Path, state_dir: Path, report_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "regularization_lab",
        "run",
        "--config",
        str(config_path),
        "--state-dir",
        str(state_dir),
        "--report",
        str(report_path),
        "--scheme",
        "ridge",
    ]


class SignalIntegrationTests(unittest.TestCase):
    def _config_path(self, root: Path) -> Path:
        return copy_sample_config_with_overrides(
            root / "experiment.yaml",
            optimizer={"max_iterations": 12, "tolerance": 0.0, "batch_size": 32},
            runtime={
                "concurrency": 4,
                "snapshot_commit_interval": 2,
                "round_delay_seconds": 0.02,
                "state_dir": str(root / "default-state"),
                "report_path": str(root / "default-report.json"),
            },
        )

    def _baseline(self, config_path: Path, root: Path) -> dict:
        config = load_experiment(config_path)
        config = replace(
            config,
            runtime=replace(
                config.runtime,
                round_delay_seconds=0.0,
                state_dir=root / "baseline-state",
                report_path=root / "baseline-report.json",
            ),
        )
        session = ExperimentSession(config)
        result = session.run_scheme("ridge")
        assert result is not None
        return result

    def test_sigterm_finishes_round_snapshots_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config_path(root)
            baseline = self._baseline(config_path, root)
            state_dir = root / "signal-state"
            report_path = root / "signal-report.json"

            process = subprocess.Popen(
                cli_command(config_path, state_dir, report_path),
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                observed_round = wait_for_round(state_dir, 2)
                process.terminate()
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, msg=stdout + stderr)
            self.assertTrue(report_path.exists())
            interrupted_report = json.loads(report_path.read_text(encoding="utf-8"))
            interrupted_result = interrupted_report["schemes"]["ridge"]
            self.assertEqual(interrupted_result["status"], "interrupted")
            self.assertGreaterEqual(interrupted_result["snapshot_version"], observed_round)

            resumed_process = subprocess.run(
                cli_command(config_path, state_dir, report_path),
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(resumed_process.returncode, 0, msg=resumed_process.stderr)
            resumed_report = json.loads(report_path.read_text(encoding="utf-8"))
            resumed_result = resumed_report["schemes"]["ridge"]
            self.assertIsNotNone(resumed_result["resumed_from_round"])
            self.assertEqual(result_core(resumed_result), result_core(baseline))

    def test_sigkill_restart_uses_latest_complete_atomic_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config_path(root)
            baseline = self._baseline(config_path, root)
            state_dir = root / "killed-state"
            report_path = root / "killed-report.json"

            process = subprocess.Popen(
                cli_command(config_path, state_dir, report_path),
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                observed_round = wait_for_round(state_dir, 4)
                process.kill()
                process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)

            resumed_process = subprocess.run(
                cli_command(config_path, state_dir, report_path),
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(resumed_process.returncode, 0, msg=resumed_process.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            resumed = report["schemes"]["ridge"]
            self.assertIsNotNone(resumed["resumed_from_round"])
            self.assertGreaterEqual(resumed["resumed_from_round"], observed_round - 2)
            self.assertEqual(result_core(resumed), result_core(baseline))
            self.assertIn(resumed["status"], {"complete"})


if __name__ == "__main__":
    unittest.main()
