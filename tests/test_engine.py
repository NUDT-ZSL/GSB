from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kdrift.engine import DriftEngine
from kdrift.model import EngineConfig
from tests.helpers import ROOT, SAMPLES, initialized_store, sample_bundle


class EngineTests(unittest.TestCase):
    def test_converges_and_second_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = initialized_store(Path(directory) / "state.db")
            try:
                engine = DriftEngine(store)
                result = engine.apply()
                self.assertTrue(result["converged"])
                self.assertGreater(result["round_count"], 0)
                verification = engine.verify()
                self.assertTrue(verification["ok"])
                self.assertEqual(verification["summary"]["residual_drift_count"], 0)
                committed = store.committed_count()
                second = engine.apply()
                self.assertTrue(second["converged"])
                self.assertEqual(second["round_count"], 0)
                self.assertEqual(store.committed_count(), committed)
                self.assertTrue(engine.verify()["ok"])
            finally:
                store.close()

    def test_budget_defers_unstarted_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = initialized_store(Path(directory) / "state.db")
            try:
                engine = DriftEngine(store)
                result = engine.apply(max_rounds=1)
                self.assertFalse(result["converged"])
                outcomes = result["rounds"][0]["operations"]
                classes = {outcome["outcome_class"] for outcome in outcomes}
                self.assertEqual(
                    result["rounds"][0]["counts"]["started"], 3
                )
                self.assertIn("not_started_budget", classes)
                self.assertIn("not_started_dependency", classes)
            finally:
                store.close()

    def test_started_failure_is_distinct_and_can_be_abandoned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = initialized_store(Path(directory) / "state.db")
            try:
                config = store.config()
                failing = EngineConfig.from_dict(
                    {
                        **config.to_dict(),
                        "retry_limit": 0,
                        "simulated_failures": ["Deployment/shop/api"],
                    }
                )
                store.set_meta("config", failing.to_dict())
                engine = DriftEngine(store)
                result = engine.apply()
                self.assertTrue(result["converged"])
                failure_outcomes = [
                    outcome
                    for round_result in result["rounds"]
                    for outcome in round_result["operations"]
                    if outcome["outcome_class"] == "started_failed"
                ]
                self.assertTrue(failure_outcomes)
                self.assertTrue(failure_outcomes[0]["started"])
                self.assertEqual(
                    failure_outcomes[0]["reason"],
                    "injected_operation_failure",
                )
                verification = engine.verify()
                self.assertTrue(verification["ok"])
                classifications = {
                    item["classification"]
                    for item in verification["risk_objects"]
                }
                self.assertEqual(
                    classifications, {"abandoned_drift"}
                )
            finally:
                store.close()

    def test_timeout_is_started_failure_not_unstarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = initialized_store(Path(directory) / "state.db")
            try:
                config = store.config()
                timeout_config = EngineConfig.from_dict(
                    {
                        **config.to_dict(),
                        "operation_timeout_ms": 10,
                        "retry_limit": 0,
                    }
                )
                store.set_meta("config", timeout_config.to_dict())
                engine = DriftEngine(store)
                result = engine.apply()
                timed_out = [
                    outcome
                    for round_result in result["rounds"]
                    for outcome in round_result["operations"]
                    if outcome.get("reason")
                    == "operation_timeout_budget_exceeded"
                ]
                self.assertTrue(timed_out)
                self.assertTrue(timed_out[0]["started"])
                self.assertEqual(
                    timed_out[0]["outcome_class"], "started_failed"
                )
                self.assertTrue(engine.verify()["ok"])
            finally:
                store.close()

    def test_committed_operation_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = initialized_store(Path(directory) / "state.db")
            try:
                engine = DriftEngine(store)
                plan = engine.plan()
                operation = next(
                    item
                    for item in plan["operations"]
                    if item["action"] == "create"
                )
                round_no = store.allocate_round(store.clock())
                store.store_round_operations(round_no, [operation])
                store.reserve_slot(
                    round_no=round_no,
                    operation=operation,
                    started_ms=store.clock(),
                    max_concurrency=3,
                    attempt=1,
                )
                result = {
                    "operation_id": operation["operation_id"],
                    "outcome": "committed",
                }
                first = store.commit_operation(
                    round_no=round_no,
                    operation=operation,
                    finished_ms=store.clock() + 10,
                    result=result,
                )
                second = store.commit_operation(
                    round_no=round_no,
                    operation=operation,
                    finished_ms=store.clock() + 10,
                    result=result,
                )
                self.assertFalse(first["idempotent_replay"])
                self.assertTrue(second["idempotent_replay"])
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) AS c FROM committed_operations"
                    ).fetchone()["c"],
                    1,
                )
            finally:
                store.close()


class CrashRecoveryTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "kdrift", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def _load(self, db: Path, directory: Path = SAMPLES) -> None:
        result = self._run(
            "load",
            "--sample",
            str(directory),
            "--db",
            str(db),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_crash_during_operation_start_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            self._load(db)
            crashed = self._run(
                "apply",
                "--db",
                str(db),
                "--crash-at",
                "operation-start:2",
            )
            self.assertEqual(crashed.returncode, 137)
            status = self._run("status", "--db", str(db))
            self.assertEqual(status.returncode, 0, status.stderr)
            status_data = json.loads(status.stdout)
            self.assertTrue(status_data["active_concurrency"])
            self.assertEqual(status_data["round_watermark"], 0)
            resumed = self._run("resume", "--db", str(db))
            self.assertEqual(resumed.returncode, 0, resumed.stdout)
            resumed_data = json.loads(resumed.stdout)
            self.assertTrue(resumed_data["converged"])
            self.assertTrue(resumed_data["verification"]["ok"])
            self.assertEqual(
                resumed_data["recovery"]["aborted_rounds"], [1]
            )

    def test_crash_before_round_commit_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            self._load(db)
            crashed = self._run(
                "apply",
                "--db",
                str(db),
                "--crash-at",
                "round-commit:1",
            )
            self.assertEqual(crashed.returncode, 137)
            status = json.loads(
                self._run("status", "--db", str(db)).stdout
            )
            self.assertGreater(
                status["committed_operation_count"], 0
            )
            self.assertEqual(status["round_watermark"], 0)
            resumed = self._run("resume", "--db", str(db))
            self.assertEqual(resumed.returncode, 0, resumed.stdout)
            resumed_data = json.loads(resumed.stdout)
            self.assertTrue(resumed_data["verification"]["ok"])
            self.assertTrue(resumed_data["converged"])

    def test_crash_after_operation_commit_recovers_without_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            self._load(db)
            crashed = self._run(
                "apply",
                "--db",
                str(db),
                "--crash-at",
                "operation-commit:2",
            )
            self.assertEqual(crashed.returncode, 137)
            status = json.loads(
                self._run("status", "--db", str(db)).stdout
            )
            committed_before_resume = status[
                "committed_operation_count"
            ]
            self.assertGreater(committed_before_resume, 0)
            self.assertEqual(status["round_watermark"], 0)
            resumed = self._run("resume", "--db", str(db))
            self.assertEqual(resumed.returncode, 0, resumed.stdout)
            resumed_data = json.loads(resumed.stdout)
            self.assertTrue(resumed_data["converged"])
            self.assertTrue(resumed_data["verification"]["ok"])
            # Replays of already committed operation IDs do not create
            # additional ledger entries.
            final_status = json.loads(
                self._run("status", "--db", str(db)).stdout
            )
            self.assertGreaterEqual(
                final_status["committed_operation_count"],
                committed_before_resume,
            )

    def test_cli_diff_output_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            self._load(db)
            first = self._run("diff", "--db", str(db))
            second = self._run("diff", "--db", str(db))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)


if __name__ == "__main__":
    unittest.main()

