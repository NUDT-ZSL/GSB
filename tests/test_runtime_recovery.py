from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from radapt.engine import RuntimeEngine
from radapt.errors import RadaptError
from radapt.parsing import load_sample_bundle, parse_bundle
from radapt.sample import sample_raw, write_sample_files
from radapt.storage import RuntimeStorage


ROOT = Path(__file__).resolve().parents[1]


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self.sample_dir = self.tmp / "sample"
        write_sample_files(self.sample_dir)
        self.bundle = load_sample_bundle(self.sample_dir)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _run_engine(self, db_name: str = "engine.sqlite3", bundle=None, **kwargs):
        db_path = self.tmp / db_name
        engine = RuntimeEngine(
            bundle or self.bundle,
            db_path,
            sample_dir=self.sample_dir,
            **kwargs,
        )
        return engine, db_path

    def test_end_to_end_drift_switch_precision_and_truth(self) -> None:
        engine, _ = self._run_engine("normal.sqlite3")
        with engine:
            report = engine.run()
        self.assertTrue(all(item["matched"] for item in report["truth_verification"]))
        self.assertEqual(len(report["drift_events"]), 1)
        event = report["drift_events"][0]
        self.assertEqual(event["table_name"], "orders")
        self.assertEqual(event["window_id"], "w1")
        self.assertEqual(event["used_observation_batch_interval"], [1])
        self.assertIn("threshold_excess", event)
        self.assertTrue(report["suppressed_drift_checks"])

        switch = report["switch_decisions"][0]
        self.assertTrue(switch["switched"])
        self.assertTrue(switch["precision_sensitive"])
        self.assertIn("error_chain", switch["precision_flags"][0])
        reasons = {item["form_id"]: item["first_rejection_reason"] for item in switch["rejected_candidates"]}
        self.assertEqual(reasons["expensive-full-rehash-v9"], "E_SWITCH_BUDGET_INSUFFICIENT")
        final = report["recovery_snapshot_after_run"]
        self.assertEqual(final["current_form_version"], 3)
        self.assertEqual([w["form_version"] for w in final["windows"]], [1, 3, 3])

    def test_switch_budget_shortfall_returns_independent_code(self) -> None:
        raw = sample_raw()
        raw["config"]["switch_cost_budget_ms"] = "0.5"
        bundle = parse_bundle(raw["table"], raw["plan"], raw["arrivals"], raw["config"])
        engine, db_path = self._run_engine("budget.sqlite3", bundle=bundle)
        with engine:
            report = engine.run()
        self.assertIn("E_SWITCH_BUDGET_INSUFFICIENT", report["business_error_codes"])
        decision = report["switch_decisions"][0]
        self.assertFalse(decision["switched"])
        self.assertEqual(decision["error_code"], "E_SWITCH_BUDGET_INSUFFICIENT")
        self.assertTrue(all(item["matched"] for item in report["truth_verification"]))
        storage = RuntimeStorage(db_path)
        try:
            self.assertEqual(storage.current_form()[0], 1)
        finally:
            storage.close()

    def test_form_version_rejects_rollback(self) -> None:
        storage = RuntimeStorage(self.tmp / "versions.sqlite3")
        initial = self.bundle.plan.initial_form
        storage.initialize_form(initial, 0)
        form_v2 = replace(initial, form_id="v2")
        storage.switch_form(form_v2, 1, requested_version=2)
        with self.assertRaises(RadaptError) as ctx:
            storage.switch_form(initial, 2, requested_version=1)
        self.assertEqual(ctx.exception.code, "E_FORM_VERSION_ROLLBACK")
        storage.close()

    def test_inprocess_crash_and_recovery_matches_oneshot(self) -> None:
        crash_db = self.tmp / "crash.sqlite3"
        engine = RuntimeEngine(
            self.bundle,
            crash_db,
            sample_dir=self.sample_dir,
            crash_after_batch=1,
            crash_mode="raise",
        )
        with self.assertRaises(RadaptError) as ctx:
            engine.run()
        self.assertEqual(ctx.exception.code, "E_CRASH_INJECTED")
        engine.close()

        recover_engine = RuntimeEngine(self.bundle, crash_db, sample_dir=self.sample_dir)
        with recover_engine:
            recovered = recover_engine.run()
        self.assertEqual(recovered["skipped_committed_batches"], [1])
        self.assertEqual([item["seq"] for item in recovered["processed_batches"]], [2, 3, 4, 5, 6])
        self.assertTrue(all(item["matched"] for item in recovered["truth_verification"]))

        oneshot_db = self.tmp / "oneshot.sqlite3"
        oneshot = RuntimeEngine(self.bundle, oneshot_db, sample_dir=self.sample_dir)
        with oneshot:
            oneshot.run()

        self._assert_window_results_equal(crash_db, oneshot_db)

    def test_sigkill_subprocess_recovery(self) -> None:
        db_path = self.tmp / "sigkill.sqlite3"
        report_kill = self.tmp / "kill-report.json"
        cmd = [
            sys.executable,
            "-m",
            "radapt.cli",
            "run",
            "--sample",
            str(self.sample_dir),
            "--db",
            str(db_path),
            "--report",
            str(report_kill),
            "--crash-after-batch",
            "2",
        ]
        killed = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(killed.returncode, -9, killed.stdout + killed.stderr)

        recover_report = self.tmp / "recover-report.json"
        cmd[-1] = "2"
        recover_cmd = [
            sys.executable,
            "-m",
            "radapt.cli",
            "run",
            "--sample",
            str(self.sample_dir),
            "--db",
            str(db_path),
            "--report",
            str(recover_report),
        ]
        recovered_proc = subprocess.run(recover_cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(recovered_proc.returncode, 0, recovered_proc.stdout + recovered_proc.stderr)
        report = json.loads(recover_report.read_text(encoding="utf-8"))
        self.assertEqual(report["skipped_committed_batches"], [1, 2])
        self.assertEqual([item["seq"] for item in report["processed_batches"]], [3, 4, 5, 6])
        self.assertTrue(all(item["matched"] for item in report["truth_verification"]))

    def _assert_window_results_equal(self, left_db: Path, right_db: Path) -> None:
        left = RuntimeStorage(left_db)
        right = RuntimeStorage(right_db)
        try:
            for window in self.bundle.plan.windows:
                left_row = left.get_window_row(window.window_id)
                right_row = right.get_window_row(window.window_id)
                self.assertIsNotNone(left_row)
                self.assertIsNotNone(right_row)
                assert left_row is not None and right_row is not None
                self.assertEqual(
                    json.loads(left_row["result_json"]),
                    json.loads(right_row["result_json"]),
                    window.window_id,
                )
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
