from __future__ import annotations

import json
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from shardcheck.io import atomic_write_json, read_json, remove_tree
from shardcheck.report import deterministic_report_view


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "shardcheck", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def wait_for_snapshot(run_dir: Path, predicate, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        path = run_dir / "state" / "snapshot.json"
        if path.exists():
            try:
                last = read_json(path)
                if predicate(last):
                    return last
            except Exception:
                pass
        time.sleep(0.02)
    raise AssertionError(f"snapshot predicate not reached, last={last!r}")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="shardcheck-tests-")
        cls.root = Path(cls.temp.name)
        cls.baseline = cls.root / "baseline"
        result = run_cli("run", "--samples", "samples", "--run-dir", str(cls.baseline), "--fresh")
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        cls.baseline_report = read_json(cls.baseline / "report.json")

        cls.injected = cls.root / "injected"
        result = run_cli(
            "run",
            "--samples",
            "samples",
            "--run-dir",
            str(cls.injected),
            "--injections",
            "samples/injections.json",
            "--fresh",
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        cls.injected_report = read_json(cls.injected / "report.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_baseline_budget_and_merge_consistency(self) -> None:
        report = self.baseline_report
        self.assertTrue(report["consistency"]["all_passed"])
        self.assertEqual(report["round_cursor"], 12)
        self.assertTrue(any(record["merge_operations"] for record in report["rounds"]))
        self.assertTrue(any(record["evictions"] for record in report["rounds"]))
        self.assertLessEqual(
            report["checkpoint_manifest"]["total_bytes"],
            report["consistency"]["global_budget_bytes"],
        )
        self.assertEqual(
            report["final_params_digest"],
            hashlib.sha256((self.baseline / "final_parameters.bin").read_bytes()).hexdigest(),
        )

    def test_all_injections_hit_expected_codes_and_rounds_continue(self) -> None:
        report = self.injected_report
        expected_codes = {
            "E_BUDGET_OCCUPANCY_OUT_OF_RANGE",
            "E_ILLEGAL_RETENTION",
            "E_SHARD_SIZE_EXCEEDED",
            "E_MERGE_FAILED",
            "E_SHARD_CONFLICT",
            "E_SHARD_GENERATION_FAILED",
            "E_SHARD_WRITE_FAILED",
        }
        observed_codes = {item["code"] for item in report["failures"]}
        self.assertEqual(observed_codes, expected_codes)
        self.assertEqual(len(report["injection_hits"]), 7)
        self.assertTrue(report["consistency"]["all_passed"])
        self.assertEqual(report["round_cursor"], 12)
        self.assertEqual(report["checkpoint_manifest"]["current_generation"], "gen-000007")
        self.assertEqual(
            report["final_params_digest"], self.baseline_report["final_params_digest"]
        )
        failed_rounds = {
            item["round"] for item in report["failures"] if item["fatal"]
        }
        self.assertTrue({6, 7, 8, 9, 10}.issubset(failed_rounds))
        for record in report["rounds"]:
            if record["round"] in {11, 12}:
                self.assertTrue(record["checkpoint"]["committed"])

    def test_snapshot_hash_mismatch_refuses_restore(self) -> None:
        run_dir = self.root / "mismatch"
        shutil.copytree(self.baseline, run_dir)
        snapshot_path = run_dir / "state" / "snapshot.json"
        snapshot = read_json(snapshot_path)
        snapshot["config_hash"] = "deliberately-wrong"
        atomic_write_json(snapshot_path, snapshot)
        result = run_cli("run", "--samples", "samples", "--run-dir", str(run_dir))
        self.assertEqual(result.returncode, 2)
        self.assertIn("E_SNAPSHOT_VERSION_MISMATCH", result.stderr)
        rejection = read_json(run_dir / "restore_rejection.json")
        self.assertEqual(rejection["code"], "E_SNAPSHOT_VERSION_MISMATCH")

    def test_storage_rejection_keeps_previous_complete_checkpoint(self) -> None:
        sample_dir = self.root / "reject-samples"
        shutil.copytree(ROOT / "samples", sample_dir)
        policy_path = sample_dir / "scheduler_policy.yaml"
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        for rule in policy["rules"]:
            rule["actions"]["budget_action"] = "reject"
        policy_path.write_text(
            yaml.safe_dump(policy, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        run_dir = self.root / "reject"
        result = run_cli("run", "--samples", str(sample_dir), "--run-dir", str(run_dir), "--fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = read_json(run_dir / "report.json")
        codes = [failure["code"] for failure in report["failures"]]
        self.assertIn("E_STORAGE_BUDGET_EXCEEDED", codes)
        self.assertEqual(report["rounds"][0]["checkpoint"]["committed"], True)
        self.assertEqual(report["rounds"][1]["checkpoint"]["committed"], False)
        self.assertEqual(report["checkpoint_manifest"]["current_generation"], "gen-000001")
        verify = run_cli("verify", "--samples", str(sample_dir), "--run-dir", str(run_dir))
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_graceful_signal_completes_round_and_resumes_to_identical_state(self) -> None:
        run_dir = self.root / "graceful"
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "shardcheck",
                "run",
                "--samples",
                "samples",
                "--run-dir",
                str(run_dir),
                "--fresh",
                "--write-delay-seconds",
                "0.08",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_snapshot(
                run_dir,
                lambda snap: snap.get("phase") in {"train", "write", "retention"}
                and snap.get("round_cursor", 0) >= 1,
            )
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.assertEqual(process.returncode, 0, stderr)
        report = read_json(run_dir / "report.json")
        self.assertEqual(report["exit"]["path"], "graceful_signal")
        self.assertEqual(report["exit"]["signal_received"], signal.SIGTERM)
        self.assertLess(report["round_cursor"], 12)
        self.assertTrue(report["consistency"]["all_passed"])

        resume = run_cli("run", "--samples", "samples", "--run-dir", str(run_dir))
        self.assertEqual(resume.returncode, 0, resume.stderr)
        resumed_report = read_json(run_dir / "report.json")
        self.assertEqual(
            deterministic_report_view(resumed_report),
            deterministic_report_view(self.baseline_report),
        )
        self.assertEqual(
            (run_dir / "final_parameters.bin").read_bytes(),
            (self.baseline / "final_parameters.bin").read_bytes(),
        )

    def test_sigkill_recovers_only_committed_manifest_state(self) -> None:
        run_dir = self.root / "forced"
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "shardcheck",
                "run",
                "--samples",
                "samples",
                "--run-dir",
                str(run_dir),
                "--fresh",
                "--write-delay-seconds",
                "0.2",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_snapshot(
                run_dir,
                lambda snap: snap.get("phase") == "train" and snap.get("round_cursor", 0) >= 1,
            )
            time.sleep(0.03)
            process.kill()
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.assertFalse((run_dir / "report.json").exists())
        self.assertEqual(read_json(run_dir / "state" / "run_state.json")["status"], "running")

        resume = run_cli("run", "--samples", "samples", "--run-dir", str(run_dir))
        self.assertEqual(resume.returncode, 0, resume.stderr)
        report = read_json(run_dir / "report.json")
        self.assertEqual(report["recovery"]["mode"], "forced_termination_resume")
        self.assertFalse(report["recovery"]["clean_previous_exit"])
        self.assertEqual(
            deterministic_report_view(report),
            deterministic_report_view(self.baseline_report),
        )
        self.assertEqual(
            (run_dir / "final_parameters.bin").read_bytes(),
            (self.baseline / "final_parameters.bin").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
