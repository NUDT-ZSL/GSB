import json
import os
import tempfile
import unittest

from layout_order.cli import main


class CliIntegrationTests(unittest.TestCase):
    def test_generate_and_all_produce_reproducible_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baseline = os.path.join(tmp, "data", "baseline.jsonl")
            sweep = os.path.join(tmp, "data", "sweep.jsonl")
            report = os.path.join(tmp, "reports", "report.json")
            snapshot_dir = os.path.join(tmp, "snapshots")
            cache_dir = os.path.join(tmp, "cache")

            generate_code = main(
                ["generate", "--baseline-out", baseline, "--sweep-out", sweep]
            )
            self.assertEqual(generate_code, 0)
            self.assertTrue(os.path.exists(baseline))
            self.assertTrue(os.path.exists(sweep))

            first_run = main(
                [
                    "all",
                    "--baseline",
                    baseline,
                    "--sweep",
                    sweep,
                    "--snapshot-dir",
                    snapshot_dir,
                    "--cache-dir",
                    cache_dir,
                    "--report",
                    report,
                ]
            )
            # 贪心页预期触发 E_ACCURACY_FLOOR，CLI 以退出码 2 表达业务码非 OK。
            self.assertEqual(first_run, 2)
            with open(report, "r", encoding="utf-8") as handle:
                first_report = handle.read()
            parsed = json.loads(first_report)
            self.assertEqual(parsed["code"], "E_ACCURACY_FLOOR")
            sweep_paths = [
                page["result"]["path"] for page in parsed["scale_sweep"]["pages"]
            ]
            self.assertIn("exact", sweep_paths)
            self.assertIn("banded", sweep_paths)
            self.assertIn("greedy", sweep_paths)

            second_run = main(
                [
                    "all",
                    "--baseline",
                    baseline,
                    "--sweep",
                    sweep,
                    "--snapshot-dir",
                    snapshot_dir,
                    "--cache-dir",
                    cache_dir,
                    "--report",
                    report,
                ]
            )
            self.assertEqual(second_run, 2)
            with open(report, "r", encoding="utf-8") as handle:
                second_report = handle.read()
            self.assertEqual(first_report, second_report)

    def test_run_without_generate_uses_existing_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baseline = os.path.join(tmp, "baseline.jsonl")
            sweep = os.path.join(tmp, "sweep.jsonl")
            main(["generate", "--baseline-out", baseline, "--sweep-out", sweep])
            report = os.path.join(tmp, "report.json")
            code = main(
                [
                    "run",
                    "--baseline",
                    baseline,
                    "--sweep",
                    sweep,
                    "--snapshot-dir",
                    os.path.join(tmp, "snap"),
                    "--cache-dir",
                    os.path.join(tmp, "cache"),
                    "--report",
                    report,
                ]
            )
            self.assertEqual(code, 2)
            self.assertTrue(os.path.exists(report))


if __name__ == "__main__":
    unittest.main()
