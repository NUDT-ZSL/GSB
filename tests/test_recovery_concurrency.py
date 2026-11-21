from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, mock_server, parse_stdout_json, run_cli, write_json, write_scene_with_port


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def _same_tenant_scene(self, port: int) -> dict:
        return {
            "scene_id": "same-tenant-parallel",
            "services": [{
                "name": "checkout",
                "address": f"http://127.0.0.1:{port}",
                "tenants": {
                    "tenant-a": {
                        "success_rate_min": 0.95,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 16,
                    }
                },
            }],
            "batches": [{
                "batch_id": "batch-a",
                "tenant": "tenant-a",
                "concurrency_gradients": [1, 2, 4, 8],
                "duration_logical_ms": 20,
                "request_pace_ms": 5,
                "allow_early_termination": False,
            }],
            "clock": {"start_ms": 0, "window_ms": 5, "step_table": [5]},
            "precision": {
                "latency_scale": 1,
                "percent_scale": 10000,
                "compare_tolerance": {"success_rate": 0.05, "p95_latency_ms": 2},
                "degraded_margin": {"success_rate": 0.005, "p95_latency_ms": 0.1},
                "error_budget": {"success_rate": 0.001, "p95_latency_ms": 2},
                "request_logical_budget_ms": 500,
            },
            "budget": {"global_logical_time_ms": 1000},
        }

    def test_crash_then_resume_matches_clean_run(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_scene_with_port(base, port)
            clean_db = base / "clean.sqlite"
            crash_db = base / "crash.sqlite"
            clean = run_cli("run", "--scene", str(scene_path), "--db", str(clean_db))
            self.assertEqual(clean.returncode, 0, clean.stderr)
            clean_sha = parse_stdout_json(clean)["report_sha256"]

            crashed = run_cli(
                "run",
                "--scene",
                str(scene_path),
                "--db",
                str(crash_db),
                "--crash-after-windows",
                "3",
            )
            self.assertEqual(crashed.returncode, 75, crashed.stderr)
            status_after_crash = parse_stdout_json(run_cli("status", "--db", str(crash_db)))
            self.assertEqual(status_after_crash["windows_committed"], 3)
            self.assertEqual(status_after_crash["unit_status_counts"].get("RUNNING"), 1)

            resumed = run_cli(
                "resume",
                "--db",
                str(crash_db),
                "--results",
                str(base / "results"),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_json = parse_stdout_json(resumed)
            self.assertEqual(resumed_json["report_sha256"], clean_sha)
            verify = run_cli("verify", "--db", str(crash_db))
            self.assertEqual(verify.returncode, 0, verify.stderr)
            report = json.loads((base / "results" / "report.json").read_text())
            self.assertEqual(len(report["windows"]), 24)
            self.assertTrue(report["analysis_complete"])

    def test_two_processes_share_sqlite_without_duplicates(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_scene_with_port(base, port)
            db_path = base / "shared.sqlite"
            clean = run_cli("run", "--scene", str(scene_path), "--db", str(base / "clean.sqlite"))
            self.assertEqual(clean.returncode, 0, clean.stderr)
            clean_sha = parse_stdout_json(clean)["report_sha256"]
            command = [
                sys.executable,
                "-m",
                "capacity_engine",
                "run",
                "--scene",
                str(scene_path),
                "--db",
                str(db_path),
            ]
            p1 = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            p2 = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            out1, err1 = p1.communicate(timeout=90)
            out2, err2 = p2.communicate(timeout=90)
            self.assertEqual(p1.returncode, 0, err1)
            self.assertEqual(p2.returncode, 0, err2)
            verify = run_cli("verify", "--db", str(db_path))
            self.assertEqual(verify.returncode, 0, verify.stderr)
            result = parse_stdout_json(verify)
            self.assertTrue(result["ok"])
            report = parse_stdout_json(run_cli("report", "--db", str(db_path)))
            self.assertEqual(len(report["windows"]), 24)
            self.assertEqual(report["budget"]["budget_used_ms"], 120)
            self.assertEqual(len({item["window_key"] for item in report["windows"]}), 24)
            first = parse_stdout_json(subprocess.CompletedProcess([], 0, out1, err1))
            second = parse_stdout_json(subprocess.CompletedProcess([], 0, out2, err2))
            self.assertEqual(
                first["windows_committed_this_process"] + second["windows_committed_this_process"],
                24,
            )
            self.assertEqual(first["report_sha256"], clean_sha)
            self.assertEqual(second["report_sha256"], clean_sha)

    def test_two_processes_process_same_tenant_different_gradients(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_json(base / "same-tenant.json", self._same_tenant_scene(port))
            clean = run_cli("run", "--scene", str(scene_path), "--db", str(base / "clean.sqlite"))
            self.assertEqual(clean.returncode, 0, clean.stderr)
            clean_sha = parse_stdout_json(clean)["report_sha256"]
            command = [
                sys.executable,
                "-m",
                "capacity_engine",
                "run",
                "--scene",
                str(scene_path),
                "--db",
                str(base / "shared.sqlite"),
            ]
            p1 = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            p2 = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out1, err1 = p1.communicate(timeout=90)
            out2, err2 = p2.communicate(timeout=90)
            self.assertEqual(p1.returncode, 0, err1)
            self.assertEqual(p2.returncode, 0, err2)
            verify = run_cli("verify", "--db", str(base / "shared.sqlite"))
            self.assertEqual(verify.returncode, 0, verify.stderr)
            report = parse_stdout_json(run_cli("report", "--db", str(base / "shared.sqlite")))
            self.assertEqual(len(report["windows"]), 16)
            self.assertEqual(report["budget"]["budget_used_ms"], 80)
            self.assertEqual(parse_stdout_json(subprocess.CompletedProcess([], 0, out1, err1))["report_sha256"], clean_sha)
            self.assertEqual(parse_stdout_json(subprocess.CompletedProcess([], 0, out2, err2))["report_sha256"], clean_sha)


if __name__ == "__main__":
    unittest.main()

