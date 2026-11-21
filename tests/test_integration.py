from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capacity_engine.engine import Engine
from capacity_engine.scene import load_scene

from tests.helpers import mock_server, parse_stdout_json, run_cli, write_scene_with_port


class IntegrationTests(unittest.TestCase):
    def test_deterministic_capacity_and_precision_flip(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_scene_with_port(base, port)
            scene = load_scene(scene_path)
            db1 = base / "run1.sqlite"
            db2 = base / "run2.sqlite"
            result1 = Engine(scene, db1).run()
            result2 = Engine(scene, db2).run()
            report1 = result1["report"]
            report2 = result2["report"]
            self.assertEqual(report1["report_sha256"], report2["report_sha256"])
            self.assertEqual(len(report1["windows"]), 24)
            self.assertEqual(result1["budget_used_ms"], 120)

            conclusions = {item["batch_id"]: item for item in report1["capacity_conclusions"]}
            self.assertEqual(conclusions["batch-a"]["status"], "INFLECTION_FOUND")
            self.assertEqual(conclusions["batch-a"]["capacity_concurrency"], 8)
            self.assertEqual(conclusions["batch-a"]["inflection_concurrency"], 16)
            self.assertEqual(conclusions["batch-a"]["high_scale"]["capacity_concurrency"], 4)
            self.assertEqual(conclusions["batch-a"]["high_scale"]["inflection_concurrency"], 8)
            self.assertEqual(conclusions["batch-b"]["status"], "NO_INFLECTION")

            marks = report1["precision_sensitive"]
            self.assertEqual(
                {(item["kind"], item["metric"], item["left_concurrency"], item["right_concurrency"]) for item in marks},
                {
                    ("gradient", "p95_latency", 4, 8),
                    ("gradient", "p95_latency", 8, 16),
                    ("gradient", "success_rate", 8, 16),
                },
            )
            for mark in marks:
                self.assertIsNotNone(mark["left_metric_ledger_seq"])
                self.assertIsNotNone(mark["right_metric_ledger_seq"])
                self.assertTrue(mark["left_windows"])
                self.assertTrue(mark["right_windows"])
                self.assertIn("num", mark["difference_error_bound_scaled"])
            self.assertTrue(any(item["higher_scale_flips"] for item in marks))

            gradients = {
                item["concurrency"]: item
                for item in report1["gradients"]
                if item["tenant"] == "tenant-a"
            }
            self.assertEqual(gradients[4]["metric"]["success_rate_scaled"], 10000)
            self.assertEqual(gradients[4]["metric"]["p95_latency_scaled"], 100)
            self.assertEqual(gradients[8]["metric"]["success_rate_scaled"], 9688)
            self.assertEqual(gradients[8]["metric"]["p95_latency_scaled"], 100)
            self.assertEqual(gradients[16]["metric"]["success_rate_scaled"], 9375)
            self.assertEqual(gradients[16]["metric"]["p95_latency_scaled"], 101)

            for row in report1["windows"]:
                self.assertGreaterEqual(row["start_ms"], 0)
            for entry in report1["ledger_entries"]:
                self.assertIn(entry["rounding_direction"], {"exact", "up", "down"})
                self.assertGreaterEqual(entry["seq"], 1)
                if entry["kind"] in {"window_success_rate", "window_p95_latency", "gradient_success_rate", "gradient_p95_latency"}:
                    self.assertIn("num", entry["discarded"])
                    self.assertIn("den", entry["cumulative_error"])

            verify = run_cli("verify", "--db", str(db1))
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertTrue(parse_stdout_json(verify)["ok"])

            report_cli = run_cli("report", "--db", str(db1))
            self.assertEqual(report_cli.returncode, 0, report_cli.stderr)
            self.assertEqual(parse_stdout_json(report_cli)["report_sha256"], report1["report_sha256"])

            status_cli = run_cli("status", "--db", str(db1))
            self.assertEqual(status_cli.returncode, 0, status_cli.stderr)
            self.assertEqual(parse_stdout_json(status_cli)["windows_committed"], 24)

            rerun = Engine(scene, db1).run()
            self.assertEqual(rerun["windows_committed_this_process"], 0)
            self.assertEqual(rerun["report_sha256"], report1["report_sha256"])
            self.assertEqual(parse_stdout_json(run_cli("status", "--db", str(db1)))["windows_committed"], 24)


if __name__ == "__main__":
    unittest.main()

