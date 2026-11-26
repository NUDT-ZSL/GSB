from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capacity_engine.engine import CapacityEngine
from capacity_engine.errors import CapacityError, ErrorCode
from capacity_engine.evaluator import assert_precision_budget
from capacity_engine.seed import generate_seed_directory
from capacity_engine.validation import load_input, shard_content_hash
from capacity_engine.windows import assign_windows, build_window_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


class CapacityEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="capacity-test-")
        cls.root = Path(cls.temp.name)
        cls.sample = generate_seed_directory(cls.root / "sample")
        cls.input_data = load_input(cls.sample)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _engine_run(self, name: str) -> tuple[Path, dict]:
        out = self.root / name
        engine = CapacityEngine(self.input_data, self.sample, out)
        return out, engine.run()

    def test_01_seed_is_deterministic_and_inputs_validate(self) -> None:
        second = self.root / "sample-second"
        generate_seed_directory(second)
        for relative in ("scenario.json", "traces.jsonl", "metric_definitions.json", "patches.json"):
            self.assertEqual(
                (self.sample / relative).read_bytes(),
                (second / relative).read_bytes(),
                relative,
            )
        self.assertEqual(360, len(self.input_data.traces))
        self.assertEqual(3, len(self.input_data.v2_shards))

    def test_02_validation_errors_are_explicit_and_write_nothing(self) -> None:
        cases = [
            ("duplicate_service", ErrorCode.VALIDATION_SCENARIO),
            ("negative_duration", ErrorCode.VALIDATION_TRACE),
            ("bad_scale", ErrorCode.VALIDATION_METRIC_DEFINITION),
            ("negative_v1_seconds", ErrorCode.VALIDATION_V1),
            ("bad_shard_hash", ErrorCode.VALIDATION_V2_SHARD_HASH),
            ("bad_generation", ErrorCode.VALIDATION_V2_GENERATION),
        ]
        for mutation, expected_code in cases:
            with self.subTest(mutation=mutation):
                bad_dir = self.root / f"invalid-{mutation}"
                shutil.copytree(self.sample, bad_dir)
                self._mutate_sample(bad_dir, mutation)
                out = self.root / f"invalid-out-{mutation}"
                result = subprocess.run(
                    [sys.executable, "-m", "capacity_engine.cli", "run", "--input", str(bad_dir), "--out", str(out)],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(expected_code, payload["error_code"])
                self.assertFalse(out.exists())

    def _mutate_sample(self, directory: Path, mutation: str) -> None:
        if mutation == "duplicate_service":
            path = directory / "scenario.json"
            data = json.loads(path.read_text())
            data["services"][1]["service_name"] = data["services"][0]["service_name"]
            path.write_text(json.dumps(data))
        elif mutation == "negative_duration":
            path = directory / "traces.jsonl"
            record = json.loads(path.read_text().splitlines()[0])
            record["duration_ms"] = -1
            path.write_text(json.dumps(record) + "\n")
        elif mutation == "bad_scale":
            path = directory / "metric_definitions.json"
            data = json.loads(path.read_text())
            data["metrics"]["cpu_utilization"]["scale"] = 0
            path.write_text(json.dumps(data))
        elif mutation == "negative_v1_seconds":
            path = directory / "thresholds" / "v1.json"
            data = json.loads(path.read_text())
            data["effective_after_seconds"] = -2
            path.write_text(json.dumps(data))
        elif mutation == "bad_shard_hash":
            path = directory / "thresholds" / "v2" / "shard-001.json"
            data = json.loads(path.read_text())
            data["generation"] = 999
            path.write_text(json.dumps(data))
        elif mutation == "bad_generation":
            path = directory / "thresholds" / "v2" / "shard-001.json"
            path = directory / "thresholds" / "v2" / "shard-001.json"
            data = json.loads(path.read_text())
            data["generation"] = 1
            data["sha256"] = shard_content_hash(data)
            path.write_text(json.dumps(data))

    def test_03_windows_are_unique_complete_and_deterministic(self) -> None:
        plan = build_window_plan(self.input_data.traces, self.input_data.metric_definitions)
        assignments, summaries = assign_windows(self.input_data.traces, plan)
        assignments_two, summaries_two = assign_windows(self.input_data.traces, plan)
        self.assertEqual(
            [item["request_seq"] for item in assignments],
            [item["request_seq"] for item in assignments_two],
        )
        self.assertEqual(
            [item["request_set_digest"] for item in summaries],
            [item["request_set_digest"] for item in summaries_two],
        )
        seqs = [item["request_seq"] for item in assignments]
        self.assertEqual(len(self.input_data.traces), len(seqs))
        self.assertEqual(set(range(1, 361)), set(seqs))
        self.assertEqual(360, sum(item["request_count"] for item in summaries))

    def test_04_full_run_determinism_precision_and_merge(self) -> None:
        out_a, summary_a = self._engine_run("run-a")
        out_b, summary_b = self._engine_run("run-b")
        self.assertEqual(summary_a["revision_count"], summary_b["revision_count"])
        for revision in summary_a["revisions"]:
            name = f"{revision['revision_id']}.json"
            self.assertEqual(
                (out_a / "reports" / name).read_bytes(),
                (out_b / "reports" / name).read_bytes(),
                name,
            )
        baseline = json.loads((out_a / "reports" / "baseline.json").read_text())
        values = baseline["evaluation"]["values"]
        self.assertEqual(72, len(values))
        for value in values:
            self.assertLessEqual(
                __import__("decimal").Decimal(value["absolute_deviation"]),
                __import__("decimal").Decimal(value["error_upper_bound_physical"])
                + __import__("decimal").Decimal("1e-48"),
            )
            self.assertTrue(value["ledger_seqs"])

        gold_cpu = next(
            decision
            for decision in baseline["threshold_merge"]["decisions"]
            if decision["threshold_key"] == "gold.cpu_utilization"
        )
        self.assertIn("source_format_priority", gold_cpu["reason"])
        self.assertEqual("v2:shard-001:threshold:gold.cpu_utilization", gold_cpu["winner_candidate_id"])

        flips = [
            record
            for record in baseline["evaluation"]["sensitive_records"]
            if record["metric"] == "latency_p95" and record["would_flip_at_higher_scale"]
        ]
        self.assertTrue(flips)
        self.assertIn("ledger_seqs", flips[0])

    def test_05_invalidation_is_precise_and_reports_are_idempotent(self) -> None:
        out, _ = self._engine_run("run-invalidation")
        before = {
            path.name: path.read_bytes()
            for path in sorted((out / "reports").glob("*.json"))
        }
        engine = CapacityEngine(self.input_data, self.sample, out)
        engine.run()
        after = {
            path.name: path.read_bytes()
            for path in sorted((out / "reports").glob("*.json"))
        }
        self.assertEqual(before, after)
        patch1 = json.loads((out / "reports" / "patch-001-gold-cpu-threshold.json").read_text())
        plan = patch1["invalidation"]
        self.assertEqual(6, len(plan["recompute"]["conclusions"]))
        self.assertEqual(27, len(plan["recompute"]["sensitivity"]))
        self.assertEqual(0, len(plan["recompute"]["metric_values"]))
        self.assertTrue(all(check["unchanged"] for check in patch1["reuse_checks"]))

    def test_06_precision_budget_error_code_keeps_ledger_seqs(self) -> None:
        out, _ = self._engine_run("run-budget")
        baseline = json.loads((out / "reports" / "baseline.json").read_text())
        bad_value = dict(baseline["evaluation"]["values"][0])
        bad_value["absolute_deviation"] = "999"
        with self.assertRaises(CapacityError) as ctx:
            assert_precision_budget([bad_value])
        self.assertEqual(ErrorCode.PRECISION_BUDGET_EXCEEDED, ctx.exception.code)
        self.assertTrue(ctx.exception.details["violations"][0]["ledger_seqs"])

    def test_07_crash_recovery_produces_identical_baseline(self) -> None:
        out = self.root / "crash-window"
        crashed = subprocess.run(
            [
                sys.executable,
                "-m",
                "capacity_engine.cli",
                "run",
                "--input",
                str(self.sample),
                "--out",
                str(out),
                "--crash-at",
                "window_aggregation",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(86, crashed.returncode)
        recovered = subprocess.run(
            [sys.executable, "-m", "capacity_engine.cli", "run", "--input", str(self.sample), "--out", str(out)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, recovered.returncode, recovered.stdout + recovered.stderr)
        clean, _ = self._engine_run("clean-for-crash-compare")
        self.assertEqual(
            (clean / "reports" / "baseline.json").read_bytes(),
            (out / "reports" / "baseline.json").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
