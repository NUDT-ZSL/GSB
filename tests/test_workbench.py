from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_drift_workbench.comparison import compare_serial_parallel
from intent_drift_workbench.canonical import sha256_json
from intent_drift_workbench.config import ConfigError
from intent_drift_workbench.engine import WorkbenchEngine
from intent_drift_workbench.loading import failure_sort_key
from intent_drift_workbench.snapshot import SnapshotManager
from scripts.generate_samples import generate


class WorkbenchAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = Path(tempfile.mkdtemp(prefix="workbench-tests-", dir=ROOT / "artifacts"))
        cls.samples = cls.temp_root / "samples"
        cls.manifest = generate(cls.samples)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temp_root, ignore_errors=True)

    def engine(
        self,
        name: str,
        *,
        execution: str = "serial",
        snapshot: bool = False,
        samples: Path | None = None,
    ) -> WorkbenchEngine:
        sample_dir = samples or self.samples
        return WorkbenchEngine(
            config_path=sample_dir / "drift_config.json",
            events_path=sample_dir / "sessions.jsonl",
            plan_path=sample_dir / "update_plan.json",
            injections_path=sample_dir / "injections.json",
            snapshot_root=(self.temp_root / name / "snapshots") if snapshot else None,
            execution=execution,
        )

    def test_prefixes_and_concurrency_are_equivalent(self) -> None:
        serial = self.engine("serial-compare", execution="serial")
        serial_result = serial.run()
        parallel = self.engine("parallel-compare", execution="parallel")
        parallel_result = parallel.run()
        self.assertTrue(serial_result["equivalence"]["equal"])
        self.assertTrue(parallel_result["equivalence"]["equal"])
        self.assertTrue(compare_serial_parallel(serial_result, parallel_result)["equal"])

        for prefix in range(len(serial.plan["batches"]) + 1):
            engine = self.engine(f"prefix-{prefix}", execution="serial")
            result = engine.run(max_batches=prefix)
            self.assertTrue(
                result["equivalence"]["equal"],
                msg=f"prefix {prefix}: {result['equivalence'].get('mismatches')}",
            )

    def test_invalidation_classes_are_exact(self) -> None:
        engine = self.engine("invalidation")
        result = engine.run()
        history = {item["batch_id"]: item for item in result["invalidation_history"]}

        turn_batch = history["B001-turn-only"]
        self.assertGreater(turn_batch["source_counts"]["turn"]["affected_windows"], 0)
        self.assertEqual(turn_batch["source_counts"]["turn"]["affected_turns"], 3)
        self.assertEqual(turn_batch["source_counts"]["vocab"]["affected_windows"], 0)
        self.assertFalse(turn_batch["source_counts"]["parameter"]["changed"])

        vocab_batch = history["B002-vocab-only"]
        self.assertEqual(vocab_batch["source_counts"]["turn"]["affected_windows"], 0)
        self.assertGreater(vocab_batch["source_counts"]["vocab"]["affected_windows"], 0)
        self.assertFalse(vocab_batch["source_counts"]["parameter"]["changed"])
        self.assertGreater(vocab_batch["feature_recompute_count"], 0)
        self.assertGreater(vocab_batch["distribution_recompute_count"], 0)

        weight_batch = history["B003-weight-only"]
        self.assertEqual(weight_batch["source_counts"]["turn"]["affected_windows"], 0)
        self.assertEqual(weight_batch["source_counts"]["vocab"]["affected_windows"], 0)
        self.assertTrue(weight_batch["source_counts"]["parameter"]["changed"])
        self.assertEqual(weight_batch["feature_recompute_count"], 0)
        self.assertEqual(weight_batch["distribution_recompute_count"], 0)
        self.assertGreater(weight_batch["scoring_recompute_count"], 0)

        mixed = history["B004-mixed"]
        self.assertGreater(mixed["source_counts"]["turn"]["affected_windows"], 0)
        self.assertGreater(mixed["source_counts"]["vocab"]["affected_windows"], 0)
        self.assertTrue(mixed["source_counts"]["parameter"]["changed"])

    def test_failures_boundary_and_injections(self) -> None:
        result = self.engine("diagnostics").run()
        self.assertTrue(result["equivalence"]["equal"])
        self.assertGreaterEqual(len(result["failures"]), 6)
        self.assertEqual(
            result["failures"],
            sorted(result["failures"], key=failure_sort_key),
        )
        codes = {item["business_code"] for item in result["failures"]}
        self.assertIn("E_TURN_INVALID", codes)
        self.assertIn("E_TURN_INTERNAL", codes)
        self.assertIn("E_WINDOW_INTERNAL", codes)
        self.assertTrue(result["boundary_alerts"])
        self.assertTrue(all(item["hit"] for item in result["injection_hits"]))
        threshold = result["threshold"]
        self.assertTrue(any(item["drift_score"] == threshold for item in result["boundary_alerts"]))

    def test_dependency_index_and_skip_audit(self) -> None:
        result = self.engine("dependencies").run()
        dependencies = result["dependency_summary"]
        vocab_terms = set(dependencies["vocab_term_refs"])
        self.assertIn("escalation", vocab_terms)
        self.assertNotIn("courier", vocab_terms)
        refs = dependencies["vocab_term_refs"]["escalation"]
        self.assertIn({"session_id": "S04-technical", "turn_no": 3}, refs)
        self.assertIn({"session_id": "S04-technical", "turn_no": 6}, refs)

        window_index = dependencies["window_dependencies"]
        skipped = [
            item for item in result["windows"]
            if item["session_id"] == "S02-shipping-gap" and item["start_turn"] == 2
        ][0]
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn(5, skipped["missing_turn_numbers"])
        self.assertIn(skipped["window_id"], window_index)

    def test_snapshot_recovery_and_config_mismatch(self) -> None:
        snapshot_root = self.temp_root / "recovery" / "snapshots"
        crashed = self.engine("recovery", snapshot=True)
        crash_result = crashed.run(crash_after_batch=2)
        self.assertTrue(crash_result["crash_requested"])
        manifests = SnapshotManager(snapshot_root).list_complete_manifests()
        self.assertEqual(manifests[-1]["global_version"], 2)
        self.assertEqual(manifests[-1]["completed_batches"], ["B001-turn-only", "B002-vocab-only"])
        incomplete = snapshot_root / "versions" / "v99999999"
        incomplete.mkdir()
        (incomplete / "turns.json").write_text("[]\n", encoding="utf-8")
        self.assertEqual(SnapshotManager(snapshot_root).list_complete_manifests()[-1]["global_version"], 2)

        resumed = WorkbenchEngine(
            config_path=self.samples / "drift_config.json",
            events_path=self.samples / "sessions.jsonl",
            plan_path=self.samples / "update_plan.json",
            injections_path=self.samples / "injections.json",
            snapshot_root=snapshot_root,
            execution="serial",
        )
        result = resumed.run()
        self.assertTrue(result["equivalence"]["equal"])
        self.assertEqual(result["recovery"]["global_version"], 2)
        self.assertEqual(result["applied_batches"][:2], ["B001-turn-only", "B002-vocab-only"])
        self.assertEqual(result["snapshot"]["global_version"], 4)
        fresh = self.engine("recovery-fresh", execution="serial").run()
        self.assertEqual(
            sha256_json({key: result[key] for key in ("turns", "windows", "failures")}),
            sha256_json({key: fresh[key] for key in ("turns", "windows", "failures")}),
        )

        mismatch_samples = self.temp_root / "mismatch-samples"
        shutil.copytree(self.samples, mismatch_samples)
        config_path = mismatch_samples / "drift_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["drift_threshold"] = config["drift_threshold"] + 0.01
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        mismatch_engine = WorkbenchEngine(
            config_path=config_path,
            events_path=mismatch_samples / "sessions.jsonl",
            plan_path=mismatch_samples / "update_plan.json",
            injections_path=mismatch_samples / "injections.json",
            snapshot_root=snapshot_root,
            execution="serial",
        )
        with self.assertRaises(ConfigError) as context:
            mismatch_engine.prepare()
        self.assertEqual(context.exception.code, "E_CONFIG_MISMATCH")

    def test_cli_forced_termination_has_no_report_then_resumes(self) -> None:
        output = self.temp_root / "cli-recovery"
        command = [
            sys.executable,
            "-m",
            "intent_drift_workbench",
            "run",
            "--samples",
            str(self.samples),
            "--output",
            str(output),
            "--execution",
            "serial",
        ]
        crashed = subprocess.run(
            command + ["--crash-after-batch", "2"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(crashed.returncode, 137, msg=crashed.stderr)
        self.assertFalse((output / "diagnostics_report.json").exists())
        payload = json.loads(crashed.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "terminated_after_snapshot")
        self.assertEqual(payload["recovery_point"]["completed_batches"], ["B001-turn-only", "B002-vocab-only"])

        resumed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(resumed.returncode, 0, msg=resumed.stderr)
        report = json.loads((output / "diagnostics_report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["equivalence"]["equal"])
        self.assertEqual(report["recovery_point"]["status"], "RECOVERED_COMPLETE_SNAPSHOT")
        self.assertEqual(report["snapshot"]["global_version"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
