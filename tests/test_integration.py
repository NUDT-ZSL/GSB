from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from reward_diag.config import load_config
from reward_diag.consistency import run_consistency_suite
from reward_diag.dependencies import load_dependencies
from reward_diag.engine import build_full_result, compare_results, run_incremental
from reward_diag.injection import run_injection_suite
from reward_diag.plan import load_plan
from reward_diag.samples import read_jsonl_entries
from reward_diag.snapshot import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
CLI = ROOT / "run_workbench.py"


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(str(SAMPLES / "feature_config.json"))
        cls.plan = load_plan(str(SAMPLES / "batch_plan.json"))
        cls.dependencies = load_dependencies(str(SAMPLES / "dependencies.json"))
        cls.base_entries = read_jsonl_entries(str(SAMPLES / "preferences.jsonl"))

    def test_incremental_invalidation_and_final_equality(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-incremental-") as state_dir:
            result = run_incremental(
                base_entries=self.base_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            full = build_full_result(
                base_entries=self.base_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
            )
            comparison = compare_results(result["canonical_result"], full)
            self.assertTrue(comparison["identical"], comparison["differences"][:5])
            self.assertTrue(result["snapshot_verification"]["valid"])
            batch_events = {
                event["batch_id"]: event
                for event in result["events"]
                if event.get("event") == "batch"
            }
            self.assertEqual(set(batch_events), {"B001", "B002", "B003", "B004"})

            b001 = batch_events["B001"]
            self.assertEqual(
                b001["feature_recomputed_pair_ids"], ["p003", "p013"]
            )
            self.assertEqual(b001["score_recomputed_pair_ids"], ["p003", "p013"])
            self.assertEqual(b001["data_changes"]["removed_pairs"], ["p005"])
            self.assertEqual(b001["recomputed_pair_counts"]["features"], 2)
            self.assertGreater(b001["reused_pair_counts"]["features"], 0)

            b002 = batch_events["B002"]
            self.assertEqual(b002["reused_stages"], ["features"])
            self.assertEqual(b002["recomputed_pair_counts"]["features"], 0)
            self.assertEqual(b002["recomputed_pair_counts"]["scoring"], 12)

            b003 = batch_events["B003"]
            self.assertEqual(b003["reused_stages"], ["features"])
            self.assertIn("scoring", b003["invalidated_stages"])
            self.assertNotIn("features", b003["invalidated_stages"])

            b004 = batch_events["B004"]
            self.assertEqual(b004["feature_recomputed_pair_ids"], ["p007", "p014"])
            self.assertEqual(b004["recomputed_pair_counts"]["scoring"], 12)
            for event in batch_events.values():
                self.assertTrue(event["invariant_checks"]["all_pass"])
                self.assertLess(
                    event["stage_versions"]["features"],
                    event["snapshot_version"] + 1,
                )

            failure_codes = {
                (failure["pair_id"], failure["business_code"])
                for failure in result["canonical_result"]["failures"]
            }
            self.assertIn(("p-no-timestamp", "E_SAMPLE_INVALID"), failure_codes)
            self.assertIn(("p-empty", "E_EMPTY_TEXT"), failure_codes)
            self.assertIn(("p010", "E_SAMPLE_INVALID"), failure_codes)

            store = SnapshotStore(state_dir)
            loaded_snapshot = store.load()
            self.assertTrue(loaded_snapshot["valid"])
            self.assertEqual(loaded_snapshot["manifest"]["cursor"], 4)

    def test_three_category_consistency_suite(self) -> None:
        result = run_consistency_suite(
            base_entries=self.base_entries,
            base_config=self.config,
            main_plan=self.plan,
            dependencies=self.dependencies,
        )
        self.assertTrue(result["all_identical"], result["errors"])
        self.assertEqual(
            set(result["covered_categories"]), {"weight_only", "data_only", "mixed"}
        )
        for scenario in result["scenarios"]:
            self.assertTrue(scenario["identical"], scenario["comparison"]["differences"][:5])

    def test_external_data_change_reuses_unaffected_features(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-external-data-") as state_dir:
            run_incremental(
                base_entries=self.base_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            changed_entries = copy.deepcopy(self.base_entries)
            changed_record = json.loads(changed_entries[0]["raw"])
            changed_record["prompt"] += " changed external words"
            changed_entries[0]["raw"] = json.dumps(
                changed_record, sort_keys=True, separators=(",", ":")
            )
            result = run_incremental(
                base_entries=changed_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            full = build_full_result(
                base_entries=changed_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
            )
            comparison = compare_results(result["canonical_result"], full)
            self.assertTrue(comparison["identical"], comparison["differences"][:5])
            self.assertEqual(result["recovery"]["status"], "external_reconcile")
            event = result["events"][0]
            self.assertEqual(event["event"], "external_reconcile")
            self.assertEqual(event["affected_pairs"], ["p001"])
            self.assertEqual(event["feature_recomputed_pair_ids"], ["p001"])
            self.assertEqual(event["reused_pair_counts"]["features"], 11)

    def test_external_scoring_config_change_reuses_all_features(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-external-config-") as state_dir:
            run_incremental(
                base_entries=self.base_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            changed_config = copy.deepcopy(self.config)
            changed_config["weights"][1] = 0.123456
            changed_config["reward_delta_threshold"] = 0.135
            result = run_incremental(
                base_entries=self.base_entries,
                base_config=changed_config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            full = build_full_result(
                base_entries=self.base_entries,
                base_config=changed_config,
                plan=self.plan,
                dependencies=self.dependencies,
            )
            comparison = compare_results(result["canonical_result"], full)
            self.assertTrue(comparison["identical"], comparison["differences"][:5])
            event = result["events"][0]
            self.assertEqual(event["recomputed_pair_counts"]["features"], 0)
            self.assertEqual(event["reused_pair_counts"]["features"], 12)
            self.assertEqual(event["reused_stages"], ["features"])
            self.assertEqual(
                event["invalidated_stages"], ["scoring", "discrimination", "summary"]
            )

    def test_feature_dimension_and_bucket_change_reextracts_all_features(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-external-shape-") as state_dir:
            run_incremental(
                base_entries=self.base_entries,
                base_config=self.config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            changed_config = copy.deepcopy(self.config)
            changed_config["feature_dimensions"] = 18
            changed_config["hash_buckets"] = 9
            changed_config["weights"] = [
                0.11, -0.07, 0.19, 0.05, -0.13, 0.23,
                0.08, -0.17, 0.14, 0.03, -0.09, 0.21,
                0.06, -0.11, 0.16, 0.02, -0.05, 0.13,
            ]
            result = run_incremental(
                base_entries=self.base_entries,
                base_config=changed_config,
                plan=self.plan,
                dependencies=self.dependencies,
                state_dir=state_dir,
            )
            full = build_full_result(
                base_entries=self.base_entries,
                base_config=changed_config,
                plan=self.plan,
                dependencies=self.dependencies,
            )
            comparison = compare_results(result["canonical_result"], full)
            self.assertTrue(comparison["identical"], comparison["differences"][:5])
            event = result["events"][0]
            self.assertTrue(event["config_changes"]["feature_projection_changed"])
            self.assertEqual(event["recomputed_pair_counts"]["features"], 12)
            self.assertEqual(event["recomputed_pair_counts"]["scoring"], 12)
            self.assertIn("features", event["invalidated_stages"])

    def test_injection_suite_hits_every_item(self) -> None:
        result = run_injection_suite(
            str(SAMPLES / "injection_config.json"),
            self.config,
            self.dependencies,
        )
        self.assertTrue(result["all_hit"])
        self.assertEqual(result["hit_count"], 7)
        ids = [item["injection_id"] for item in result["items"]]
        self.assertEqual(ids, [f"INJ-{index:03d}" for index in range(1, 8)])

    def test_forced_termination_recovers_only_from_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-recovery-") as temp:
            state_dir = os.path.join(temp, "state")
            report = os.path.join(temp, "report.json")
            common = [
                sys.executable,
                str(CLI),
                "--state-dir",
                state_dir,
                "--report",
                report,
                "--skip-full-comparison",
                "--skip-injections",
                "--skip-consistency-suite",
                "--json",
            ]
            crash = subprocess.run(
                common + ["--simulate-crash-after-stage", "features"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(crash.returncode, 137, crash.stderr)
            with open(os.path.join(state_dir, "manifest.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["cursor"], 0)
            version_entries = os.listdir(os.path.join(state_dir, "versions"))
            self.assertTrue(any(name.startswith(".staging-") for name in version_entries))

            restarted = subprocess.run(
                common,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            with open(report, encoding="utf-8") as handle:
                report_data = json.load(handle)
            self.assertEqual(report_data["recovery"]["status"], "resumed")
            self.assertTrue(report_data["recovery"]["stage_hashes_verified"])
            self.assertTrue(report_data["recovery"]["ignored_orphan_versions"])
            self.assertEqual(report_data["snapshot"]["cursor"], 4)
            self.assertTrue(report_data["checks"]["snapshot_stage_hashes_verified"])

    @unittest.skipIf(os.name == "nt", "POSIX signal assertion")
    def test_sigterm_finishes_current_batch_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reward-diag-signal-") as temp:
            state_dir = os.path.join(temp, "state")
            report = os.path.join(temp, "report.json")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(CLI),
                    "--state-dir",
                    state_dir,
                    "--report",
                    report,
                    "--simulate-batch-delay",
                    "1.0",
                    "--skip-full-comparison",
                    "--skip-injections",
                    "--skip-consistency-suite",
                    "--json",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            manifest_path = os.path.join(state_dir, "manifest.json")
            deadline = time.time() + 15
            while time.time() < deadline and not os.path.exists(manifest_path):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(manifest_path), "baseline manifest absent")
            time.sleep(0.2)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            with open(report, encoding="utf-8") as handle:
                report_data = json.load(handle)
            self.assertTrue(report_data["run"]["stopped_early"])
            self.assertEqual(report_data["run"]["exit_code"], 0)
            self.assertGreaterEqual(report_data["snapshot"]["cursor"], 1)
            self.assertLessEqual(report_data["snapshot"]["cursor"], 4)
            self.assertIn(15, report_data["run"]["signals"]["received"])


if __name__ == "__main__":
    unittest.main()
