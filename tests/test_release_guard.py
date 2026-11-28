from __future__ import annotations

import copy
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from release_guard.cli import main as cli_main
from release_guard.common import canonical_core, stable_json
from release_guard.comparison import first_reference_mismatch
from release_guard.engine import run_main
from release_guard.generator import SCHEMA_NAMES, generate_schemas
from release_guard.normalizer import normalize_release
from release_guard.pipeline import run_pipeline
from release_guard.reference_runner import run_reference


SEED = 20261127


class ReleaseGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = generate_schemas(SEED)
        cls.projects = {
            name: normalize_release(cls.schemas[name]) for name in SCHEMA_NAMES
        }
        cls.runs = {name: run_main(cls.projects[name]) for name in SCHEMA_NAMES}
        cls.references = {
            name: run_reference(cls.projects[name]) for name in SCHEMA_NAMES
        }

    def test_generator_is_deterministic_and_versions_are_distinct(self) -> None:
        again = generate_schemas(SEED)
        self.assertEqual(stable_json(self.schemas), stable_json(again))
        self.assertEqual(set(self.schemas), set(SCHEMA_NAMES))
        self.assertEqual(self.schemas["v1"]["schema"], "gsb.release/v1")
        self.assertEqual(self.schemas["v2"]["schema"], "gsb.release/v2")
        self.assertEqual(self.schemas["v3"]["schema"], "gsb.release/v3")
        self.assertEqual(
            self.schemas["v1"]["rollout"]["steps"][0]["traffic_percent"], 5.0
        )
        self.assertEqual(
            self.schemas["v2"]["stages"][1]["traffic_fraction"], 0.05
        )
        self.assertEqual(
            self.schemas["v3"]["plan"]["traffic_steps"][0]["traffic_ppm"],
            50000,
        )

    def test_three_schema_versions_normalize_to_same_core(self) -> None:
        cores = {
            name: stable_json(canonical_core(project))
            for name, project in self.projects.items()
        }
        self.assertEqual(len(set(cores.values())), 1)
        first_steps = [self.projects[name]["steps"][0] for name in SCHEMA_NAMES]
        self.assertEqual(
            stable_json(first_steps[0]), stable_json(first_steps[1])
        )
        self.assertEqual(
            stable_json(first_steps[1]), stable_json(first_steps[2])
        )
        self.assertEqual(first_steps[0]["traffic_ppm"], 50000)
        self.assertEqual(first_steps[0]["thresholds"]["error_rate"], "0.01")
        self.assertEqual(first_steps[0]["thresholds"]["latency_p95_ms"], "320")
        self.assertEqual(
            first_steps[0]["thresholds"]["session_drop_rate"], "0.008"
        )

    def test_provenance_records_source_units_and_expressions(self) -> None:
        v1 = self.projects["v1"]["provenance"]["steps[1].traffic_ppm"]
        v2 = self.projects["v2"]["provenance"]["steps[1].traffic_ppm"]
        v3 = self.projects["v3"]["provenance"]["steps[1].traffic_ppm"]
        self.assertEqual(v1["raw_unit"], "percent")
        self.assertEqual(v2["raw_unit"], "fraction")
        self.assertEqual(v3["raw_unit"], "ppm")
        self.assertEqual(
            {v1["normalized_value"], v2["normalized_value"], v3["normalized_value"]},
            {50000},
        )
        expr = self.projects["v3"]["provenance"][
            "steps[1].thresholds.error_rate"
        ]
        self.assertEqual(expr["difference_kind"], "threshold_expression")
        self.assertEqual(expr["raw_value"], "<= 1.00%")
        self.assertEqual(expr["normalized_value"], "0.01")

    def test_isolated_spike_is_not_a_rollback(self) -> None:
        run = self.runs["v1"]
        step1 = run["step_decisions"][0]
        metrics = run["ledger"][0]["metrics"]["error_rate"]
        self.assertEqual(step1["verdict"], "promote")
        self.assertEqual(metrics["breach_count"], 2)
        self.assertEqual(metrics["spike_count"], 1)
        self.assertEqual(metrics["sustained_breach_count"], 0)

    def test_missing_probe_pauses_without_stale_value_and_resets_streak(self) -> None:
        run = self.runs["v1"]
        step2 = run["step_decisions"][1]
        self.assertEqual(step2["verdict"], "promote")
        self.assertEqual(len(step2["pause_evidence"]), 1)
        self.assertEqual(step2["pause_evidence"][0]["logical_time_seconds"], 330)
        self.assertEqual(step2["resume_evidence"][0]["logical_time_seconds"], 480)
        self.assertEqual(run["ledger"][1]["pause_count"], 1)
        self.assertEqual(run["ledger"][1]["paused_seconds"], 150)

        latency = run["metric_trajectories"]["step-2"]["latency_p95_ms"]
        by_time = {point["window"]["end_inclusive"]: point for point in latency}
        self.assertEqual(by_time[300]["consecutive_breaches"], 2)
        for timestamp in (330, 360, 390, 420, 450):
            point = by_time[timestamp]
            self.assertTrue(point["missing_degraded"])
            self.assertIsNone(point["value"])
            self.assertEqual(point["consecutive_breaches"], 0)
            self.assertEqual(point["used_sample_times"], [])
        self.assertFalse(by_time[480]["missing_degraded"])
        self.assertIsNotNone(by_time[480]["value"])
        self.assertEqual(by_time[480]["consecutive_breaches"], 0)

    def test_sustained_breach_rolls_back_to_recent_stable_step(self) -> None:
        run = self.runs["v1"]
        step4 = run["step_decisions"][3]
        trigger = step4["trigger"]
        self.assertEqual(step4["verdict"], "rollback")
        self.assertEqual(trigger["metric"], "error_rate")
        self.assertEqual(trigger["threshold"], "0.01")
        self.assertEqual(trigger["observed"], 0.065)
        self.assertEqual(trigger["consecutive_breaches"], 3)
        self.assertEqual(trigger["required_consecutive_breaches"], 3)
        self.assertEqual(step4["rollback_target"]["step_number"], 3)
        self.assertEqual(run["rollback"]["traffic_recovery_percent"], "30")
        self.assertEqual(run["rollback"]["affected_session_count"], 3600)
        self.assertEqual(
            [action["action"] for action in run["rollback"]["actions"]],
            [
                "freeze_rollout",
                "switch_traffic",
                "drain_candidate_sessions",
                "reobserve_stable_step",
                "mark_release_rolled_back",
            ],
        )
        self.assertEqual(run["step_decisions"][4]["verdict"], "not_evaluated")

    def test_ledger_and_step_decisions_are_one_to_one(self) -> None:
        run = self.runs["v1"]
        self.assertEqual(len(run["ledger"]), len(run["step_decisions"]))
        for decision, entry in zip(run["step_decisions"], run["ledger"]):
            self.assertEqual(decision["ledger_ref"], entry["entry_id"])
            self.assertEqual(decision["step_number"], entry["step_number"])
            self.assertEqual(decision["verdict"], entry["verdict"])
            self.assertEqual(entry["pause_count"], len(decision["pause_evidence"]))

    def test_reference_runner_matches_every_schema(self) -> None:
        for schema in SCHEMA_NAMES:
            self.assertIsNone(
                first_reference_mismatch(
                    self.runs[schema], self.references[schema]
                )
            )
        self.assertEqual(
            len(
                {
                    stable_json(self.references[name]["step_decisions"])
                    for name in SCHEMA_NAMES
                }
            ),
            1,
        )

    def test_reference_mismatch_reports_first_step_and_metric_basis(self) -> None:
        broken = copy.deepcopy(self.runs["v1"])
        broken["step_decisions"][0]["verdict"] = "rollback"
        mismatch = first_reference_mismatch(broken, self.references["v1"])
        self.assertIsNotNone(mismatch)
        assert mismatch is not None
        self.assertEqual(mismatch["step_number"], 1)
        self.assertEqual(mismatch["field"], "verdict")
        self.assertIsNotNone(mismatch["metric_basis"]["main"])
        self.assertIsNotNone(mismatch["metric_basis"]["reference"])

    def test_full_pipeline_writes_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = run_pipeline(
                workdir=root,
                seed=SEED,
                samples_dir=root / "samples",
                output_dir=root / "artifacts",
            )
            self.assertTrue(summary["all_passed"])
            for schema in SCHEMA_NAMES:
                for key in (
                    "normalized",
                    "run",
                    "reference",
                    "step_decisions_json",
                    "step_decisions_csv",
                    "metric_trajectories",
                    "rollback_actions",
                    "ledger_json",
                    "ledger_csv",
                ):
                    self.assertTrue((root / summary["files"][schema][key]).is_file())
            self.assertTrue((root / "artifacts" / "verification.json").is_file())
            self.assertTrue(
                (root / "artifacts" / "cross_version_differences.json").is_file()
            )
            self.assertEqual(len(summary["hashes"]["inputs"]["v1"]), 64)
            with (root / "artifacts" / "v1" / "ledger.csv").open(
                encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 5)

    def test_cli_returns_success_and_is_quiet_capable(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                code = cli_main(
                    [
                        "--quiet",
                        "--seed",
                        str(SEED),
                        "--samples-dir",
                        "samples",
                        "--output-dir",
                        "artifacts",
                    ]
                )
                self.assertEqual(code, 0)
                verification = json.loads(
                    Path("artifacts/verification.json").read_text(encoding="utf-8")
                )
                self.assertTrue(verification["all_passed"])
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()

