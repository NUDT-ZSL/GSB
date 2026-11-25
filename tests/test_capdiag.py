from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from capdiag.aggregation import aggregate_trace
from capdiag.cli import main as cli_main
from capdiag.engine import run_service
from capdiag.errors import CapDiagError, ErrorCode
from capdiag.fixedpoint import Fraction
from capdiag.generate import generate_sample_workspace
from capdiag.jsonutil import dumps
from capdiag.metrics import calculate_fixed_metric, compare_values, decimal_truth_metric
from capdiag.storage import connect, recover, stage_window_bundle
from capdiag.thresholds import (
    ThresholdCandidate,
    adjudicate_pair,
    load_v1_thresholds,
    load_v2_thresholds,
    merge_thresholds,
)
from capdiag.validation import load_metric_config, load_scenario, load_trace
from capdiag.verify import full_verify


REPO_ROOT = Path(__file__).resolve().parents[1]


class SampleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.paths = generate_sample_workspace(self.root)
        self.samples = self.root / "samples"
        self.scenario = load_scenario(self.samples / "scenario.json")
        self.trace = load_trace(self.samples / "traces.jsonl", self.scenario)
        self.metrics = load_metric_config(self.samples / "metrics.json")

    def tearDown(self) -> None:
        self._temp.cleanup()


class ValidationTests(SampleTestBase):
    def test_clean_sample(self) -> None:
        self.assertEqual(len(self.scenario.services), 3)
        self.assertEqual(len(self.trace.errors), 0)
        self.assertEqual(len(self.trace.records), 1500)

    def test_invalid_trace_error_codes_are_isolated(self) -> None:
        bad = load_trace(self.samples / "invalid_traces.jsonl", self.scenario)
        codes = {error["error_code"] for error in bad.errors}
        self.assertIn(ErrorCode.TRACE_SEQUENCE, codes)
        self.assertIn(ErrorCode.TRACE_CLOCK, codes)
        self.assertIn(ErrorCode.SCHEMA, codes)
        self.assertTrue(bad.records)
        self.assertEqual(bad.records[0].instance_id, "alpha-1")

    def test_duplicate_instance_code(self) -> None:
        data = json.loads((self.samples / "scenario.json").read_text())
        data["services"][1]["instances"][0]["instance_id"] = "alpha-1"
        path = self.root / "duplicate-scenario.json"
        path.write_text(json.dumps(data))
        with self.assertRaises(CapDiagError) as ctx:
            load_scenario(path)
        self.assertEqual(ctx.exception.code, ErrorCode.DUPLICATE_INSTANCE)

    def test_bad_scale_uses_dedicated_code(self) -> None:
        data = json.loads((self.samples / "metrics.json").read_text())
        data["metrics"]["cpu_utilization"]["scale"] = 0
        path = self.root / "bad-metrics.json"
        path.write_text(json.dumps(data))
        with self.assertRaises(CapDiagError) as ctx:
            load_metric_config(path)
        self.assertEqual(ctx.exception.code, ErrorCode.METRIC_SCALE)


class WindowAndFixedPointTests(SampleTestBase):
    def test_unique_deterministic_windows(self) -> None:
        first = aggregate_trace(
            self.trace.records,
            origin_ms=0,
            length_ms=10_000,
            step_ms=10_000,
            expected_window_count=6,
            service_filter="svc-alpha",
        )
        second = aggregate_trace(
            self.trace.records,
            origin_ms=0,
            length_ms=10_000,
            step_ms=10_000,
            expected_window_count=6,
            service_filter="svc-alpha",
        )
        self.assertEqual(set(first), set(second))
        for key, buckets in first.items():
            self.assertEqual(len(buckets), 6)
            for index, aggregate in buckets.items():
                again = second[key][index]
                self.assertEqual(dumps(aggregate.summary()), dumps(again.summary()))
                self.assertEqual(len(aggregate.assignment_keys), aggregate.request_count)

        alpha_instances = [key for key in first if key.startswith("instance:")]
        total = sum(first[key][i].request_count for key in alpha_instances for i in range(6))
        self.assertEqual(total, 720)

    def test_fixed_values_match_truth_within_propagated_bound(self) -> None:
        aggregates = aggregate_trace(
            self.trace.records,
            origin_ms=0,
            length_ms=10_000,
            step_ms=10_000,
            expected_window_count=6,
            service_filter="svc-alpha",
        )
        for scope_key, buckets in aggregates.items():
            scope_type, scope_id = scope_key.split(":", 1)
            rate = Decimal(6) if scope_type == "instance" else Decimal(12)
            for metric_name, definition in self.metrics["metrics"].items():
                carry = Fraction(0, 1)
                for index in range(6):
                    aggregate = buckets[index]
                    result = calculate_fixed_metric(
                        metric_name, definition, aggregate, rate, carry
                    )
                    truth = decimal_truth_metric(
                        metric_name, definition, aggregate, rate
                    )
                    comparison = compare_values(
                        fixed_value=result.fixed_value,
                        scale=definition["scale"],
                        truth=truth,
                        error_bound=result.error_bound,
                    )
                    self.assertTrue(
                        comparison["within_error_budget"],
                        f"{scope_id} {index} {metric_name}: {comparison}",
                    )
                    carry = result.carry_out


class ThresholdTests(SampleTestBase):
    def test_merge_adjudication(self) -> None:
        v1 = load_v1_thresholds(self.samples / "thresholds_v1.json")
        v2 = load_v2_thresholds(self.samples / "thresholds_v2_metadata.sqlite")
        view = merge_thresholds(v1, v2)
        decisions = {item["key"]: item for item in view["decisions"]}

        alpha = decisions["svc-alpha:cpu_utilization:warn"]
        self.assertIn("same_logical_clock_source_priority", alpha["decision"])
        self.assertEqual(alpha["winner"]["source_version"], "v2")

        beta_memory = decisions["svc-beta:memory_utilization:warn"]
        self.assertIn("same_logical_clock_source_priority", beta_memory["decision"])
        self.assertEqual(beta_memory["winner"]["source_version"], "v2")
        v1_beta = next(
            candidate for candidate in v1
            if candidate.key() == ("svc-beta", "memory_utilization", "warn")
        )
        self.assertEqual(
            v1_beta.original_time_repr["conversion"]["scale_ms_per_second"],
            1000,
        )
        tombstone = decisions["svc-gamma:latency_p95_ms:warn"]
        self.assertIsNone(tombstone["winner"])
        self.assertIn("tombstoned", tombstone["decision"])

        older = ThresholdCandidate(
            scope="svc-x", metric="cpu_utilization", level="warn",
            value=Decimal("0.7"), effective_logical_ms=0, generation=9,
            source_version="v2", snapshot_id="old", original_time_repr={},
        )
        newer = ThresholdCandidate(
            scope="svc-x", metric="cpu_utilization", level="warn",
            value=Decimal("0.6"), effective_logical_ms=5000, generation=1,
            source_version="v1", snapshot_id="new-clock", original_time_repr={},
        )
        winner, basis = adjudicate_pair([older, newer])
        self.assertIn("logical_clock_newer", basis)
        self.assertEqual(winner.source_version, "v1")

    def test_tampered_v2_hash(self) -> None:
        shard = self.samples / "thresholds_v2" / "shard_000.json"
        data = json.loads(shard.read_text())
        data["tampered"] = True
        shard.write_text(json.dumps(data))
        with self.assertRaises(CapDiagError) as ctx:
            load_v2_thresholds(self.samples / "thresholds_v2_metadata.sqlite")
        self.assertEqual(ctx.exception.code, ErrorCode.V2_HASH)


class EndToEndTests(SampleTestBase):
    def _cli(self, *arguments: str) -> int:
        return cli_main(list(arguments))

    def test_run_verify_corruption_budget_and_recovery_stage(self) -> None:
        db = self.root / "capdiag.sqlite"
        report = self.root / "verify.json"
        self.assertEqual(
            0,
            self._cli(
                "apply-threshold",
                "--db", str(db),
                "--scenario", str(self.samples / "scenario.json"),
                "--metrics", str(self.samples / "metrics.json"),
                "--v1", str(self.samples / "thresholds_v1.json"),
                "--v2-metadata", str(self.samples / "thresholds_v2_metadata.sqlite"),
            ),
        )
        self.assertEqual(
            0,
            self._cli(
                "run",
                "--db", str(db),
                "--scenario", str(self.samples / "scenario.json"),
                "--traces", str(self.samples / "traces.jsonl"),
                "--metrics", str(self.samples / "metrics.json"),
                "--service", "svc-alpha",
            ),
        )
        self.assertEqual(
            0,
            self._cli(
                "verify",
                "--db", str(db),
                "--scenario", str(self.samples / "scenario.json"),
                "--traces", str(self.samples / "traces.jsonl"),
                "--metrics", str(self.samples / "metrics.json"),
                "--json", str(report),
            ),
        )
        verified = json.loads(report.read_text())
        self.assertTrue(verified["all_within_error_budget"])
        self.assertEqual(verified["checked_metric_values"], 72)

        conn = connect(db)
        bundle = {
            "scope_id": "svc-alpha",
            "window_index": 99,
            "window_version": "test-version",
            "ledgers": {"cpu_utilization": {"carry_out_num": 0, "carry_out_den": 1}},
        }
        prep_id = stage_window_bundle(conn, bundle)
        recovery = recover(conn)
        self.assertTrue(any(p["prep_id"] == prep_id for p in recovery["window_preps"]))
        conn.close()

        raw = sqlite3.connect(db)
        raw.execute("UPDATE metric_values SET fixed_value=999999 WHERE metric='cpu_utilization'")
        raw.commit()
        raw.close()
        exit_code = self._cli(
            "verify",
            "--db", str(db),
            "--scenario", str(self.samples / "scenario.json"),
            "--traces", str(self.samples / "traces.jsonl"),
            "--metrics", str(self.samples / "metrics.json"),
            "--json", str(self.root / "bad-verify.json"),
        )
        self.assertEqual(exit_code, 3)
        bad = json.loads((self.root / "bad-verify.json").read_text())
        self.assertTrue(bad["budget_violations"])
        self.assertTrue(bad["budget_violations"][0]["register_ids"])

    def test_sigkill_stage_replay(self) -> None:
        db = self.root / "kill.sqlite"
        common = [
            "--db", str(db),
            "--scenario", str(self.samples / "scenario.json"),
            "--traces", str(self.samples / "traces.jsonl"),
            "--metrics", str(self.samples / "metrics.json"),
        ]
        self._cli(
            "apply-threshold",
            "--db", str(db),
            "--scenario", str(self.samples / "scenario.json"),
            "--metrics", str(self.samples / "metrics.json"),
            "--v1", str(self.samples / "thresholds_v1.json"),
            "--v2-metadata", str(self.samples / "thresholds_v2_metadata.sqlite"),
        )
        killed = subprocess.run(
            [
                sys.executable,
                "-m",
                "capdiag",
                "run",
                *common,
                "--service",
                "svc-alpha",
                "--crash-at",
                "stage",
                "--crash-window",
                "2",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(killed.returncode, -9)
        recovered = self._cli("recover", "--db", str(db))
        self.assertEqual(recovered, 0)
        self.assertEqual(self._cli("run", *common, "--service", "svc-alpha"), 0)

        conn = sqlite3.connect(db)
        duplicate_windows = conn.execute(
            "SELECT count(*) FROM (SELECT scope_id,window_index,window_version,count(*) n "
            "FROM windows GROUP BY 1,2,3 HAVING n>1)"
        ).fetchone()[0]
        duplicate_ledgers = conn.execute(
            "SELECT count(*) FROM (SELECT window_pk,metric,metric_version,count(*) n "
            "FROM remainder_ledger GROUP BY 1,2,3 HAVING n>1)"
        ).fetchone()[0]
        self.assertEqual(duplicate_windows, 0)
        self.assertEqual(duplicate_ledgers, 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM windows").fetchone()[0], 18)


if __name__ == "__main__":
    unittest.main()
