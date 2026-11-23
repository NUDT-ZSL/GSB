from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from elastic_control.engine import apply_patch, decide_round, load_workspace, resume
from elastic_control.errors import ControlError
from elastic_control.samplegen import generate_samples
from elastic_control.store import Store


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def _load(self, root: Path) -> tuple[Path, Store]:
        data = root / "data"
        generate_samples(data)
        database = root / "control.db"
        load_workspace(data_dir=data, db_path=database, reset=True)
        return database, Store(database)

    def test_full_sample_rounds_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self._load(Path(temporary))
            round_status = {}
            for round_id, at_ms in (("r1", 2000), ("r2", 5000), ("r3", 8000), ("r4", 9000)):
                result = decide_round(
                    store,
                    round_id=round_id,
                    at_ms=at_ms,
                    expected_version=1,
                )
                round_status[round_id] = result["status"]
            status = store.status()
            self.assertEqual(status["budget"]["used_cpu_core_ms"], "14250")
            r1 = {
                action["workload"]: action
                for action in round_status["r1"]["actions"]
                if action["round_id"] == "r1"
            }
            self.assertEqual(r1["api-web"]["status"], "succeeded")
            self.assertEqual(r1["worker"]["status"], "failed_after_start")
            self.assertEqual(r1["batch"]["status"], "succeeded")
            self.assertEqual(r1["archive"]["status"], "abandoned")
            self.assertEqual(status["workloads"]["api-web"]["replicas"], 8)
            self.assertEqual(status["workloads"]["worker"]["replicas"], 8)
            self.assertEqual(status["workloads"]["batch"]["replicas"], 3)
            self.assertEqual(status["workloads"]["archive"]["replicas"], 2)
            self.assertEqual(store.verify()["ok"], True)

            repeated = decide_round(
                store, round_id="r4", at_ms=9000, expected_version=1
            )
            self.assertTrue(repeated["commit"]["idempotent"])
            self.assertEqual(repeated["execution"]["status"], "no_pending_actions")
            with self.assertRaises(ControlError) as caught:
                decide_round(store, round_id="r4", at_ms=9000, expected_version=0)
            self.assertEqual(caught.exception.code, "VERSION_ROLLBACK")

    def test_patch_calculates_closure_and_preserves_unaffected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self._load(Path(temporary))
            decide_round(store, round_id="r1", at_ms=2000, expected_version=1)
            before = store.status()
            result = apply_patch(
                store,
                {
                    "base_version": 1,
                    "operations": [
                        {
                            "op": "update",
                            "kind": "metric",
                            "name": "api_cpu",
                            "value": {"target_value": 0.65},
                        }
                    ],
                },
            )
            after = store.status()
            self.assertEqual(result["policy_version"], 2)
            self.assertEqual(
                sorted(result["impact"]["affected_workloads"]), ["api-web"]
            )
            self.assertIn("worker", result["impact"]["reused_workloads"])
            for name in ("worker", "batch", "archive"):
                self.assertEqual(before["workloads"][name], after["workloads"][name])
            self.assertIsNone(after["workloads"]["api-web"]["target_cache"])
            self.assertNotIn("api_cpu", after["metric_ledgers"])
            self.assertTrue(store.verify()["ok"])
            post_patch = decide_round(
                store,
                round_id="post-patch",
                at_ms=9000,
                expected_version=2,
            )
            self.assertFalse(post_patch["commit"]["idempotent"])
            self.assertTrue(store.verify()["ok"])

    def test_forced_termination_recovery_does_not_recharge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, store = self._load(root)
            command = [
                sys.executable,
                "-m",
                "elastic_control",
                "--db",
                str(database),
                "decide",
                "--round-id",
                "crash-r1",
                "--at-ms",
                "2000",
                "--expected-version",
                "1",
                "--crash-after-start",
            ]
            crashed = subprocess.run(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(crashed.returncode, 97, crashed.stderr)
            interrupted = store.status()
            self.assertEqual(interrupted["budget"]["used_cpu_core_ms"], "7500")
            self.assertEqual(
                sorted(
                    action["workload"]
                    for action in interrupted["actions"]
                    if action["status"] == "started"
                ),
                ["api-web", "batch", "worker"],
            )
            resumed = resume(store)
            self.assertEqual(resumed["resumed_rounds"], ["crash-r1"])
            status = resumed["status"]
            self.assertEqual(status["budget"]["used_cpu_core_ms"], "7500")
            self.assertEqual(status["workloads"]["api-web"]["replicas"], 5)
            self.assertEqual(status["workloads"]["batch"]["replicas"], 4)
            self.assertEqual(status["workloads"]["worker"]["replicas"], 4)
            before = json.dumps(status, sort_keys=True)
            second = resume(store)
            self.assertEqual(second["resumed_rounds"], [])
            self.assertEqual(json.dumps(store.status(), sort_keys=True), before)
            self.assertTrue(store.verify()["ok"])

    def test_two_processes_share_round_without_oversell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, store = self._load(root)
            base = [
                sys.executable,
                "-m",
                "elastic_control",
                "--db",
                str(database),
                "decide",
                "--round-id",
                "r1",
                "--at-ms",
                "2000",
                "--expected-version",
                "1",
            ]
            first = subprocess.Popen(
                base + ["--workload", "api-web"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                base
                + [
                    "--workload",
                    "worker",
                    "--workload",
                    "batch",
                    "--workload",
                    "archive",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            _, first_err = first.communicate()
            _, second_err = second.communicate()
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            status = store.status()
            self.assertEqual(status["budget"]["used_cpu_core_ms"], "7500")
            self.assertLessEqual(
                int(status["budget"]["used_cpu_core_ms"]),
                int(status["budget"]["limits"]["pool_cpu_core_ms"]),
            )
            self.assertEqual(
                len(
                    [
                        action
                        for action in status["actions"]
                        if action["round_id"] == "r1"
                    ]
                ),
                4,
            )
            self.assertTrue(store.verify()["ok"])

    def test_action_result_is_idempotent_and_conflicts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self._load(Path(temporary))
            result = decide_round(
                store, round_id="r1", at_ms=2000, expected_version=1
            )
            action = next(
                item
                for item in result["status"]["actions"]
                if item["workload"] == "api-web"
            )
            before = store.status()
            repeated = store.complete_action(
                action["action_key"],
                success=True,
                finish_at_ms=3000,
            )
            self.assertTrue(repeated["idempotent"])
            after = store.status()
            self.assertEqual(
                before["budget"]["used_cpu_core_ms"],
                after["budget"]["used_cpu_core_ms"],
            )
            self.assertEqual(
                before["workloads"]["api-web"]["replicas"],
                after["workloads"]["api-web"]["replicas"],
            )
            with self.assertRaises(ControlError) as caught:
                store.complete_action(
                    action["action_key"],
                    success=False,
                    finish_at_ms=3000,
                )
            self.assertEqual(caught.exception.code, "ACTION_RESULT_CONFLICT")

    def test_verify_detects_tampered_budget_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, store = self._load(Path(temporary))
            decide_round(store, round_id="r1", at_ms=2000, expected_version=1)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE budget_account SET cpu_used_text='99999' WHERE id=1"
                )
            result = store.verify()
            self.assertFalse(result["ok"])
            codes = {item["code"] for item in result["violations"]}
            self.assertIn("BUDGET_CPU_LEDGER_MISMATCH", codes)
            self.assertIn("CPU_BUDGET_OVERSELL", codes)

    def test_first_precision_divergence_is_persisted_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self._load(Path(temporary))
            apply_patch(
                store,
                {
                    "base_version": 1,
                    "operations": [
                        {
                            "op": "update",
                            "kind": "budget",
                            "name": "global",
                            "value": {"float_tolerance": "0.000001"},
                        }
                    ],
                },
            )
            result = decide_round(
                store, round_id="strict-r1", at_ms=2000, expected_version=2
            )
            divergence = result["status"]["first_precision_divergence"]
            self.assertIsNotNone(divergence)
            assert divergence is not None
            self.assertTrue(divergence["metric"])
            self.assertIn("fixed_value", divergence)
            self.assertIn("decimal_value", divergence)
            self.assertTrue(divergence["step_id"])
            self.assertTrue(store.verify()["ok"])

    def test_round_clock_rollback_is_rejected_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self._load(Path(temporary))
            decide_round(store, round_id="r1", at_ms=2000, expected_version=1)
            before = store.status()
            with self.assertRaises(ControlError) as caught:
                decide_round(
                    store,
                    round_id="backwards",
                    at_ms=1900,
                    expected_version=1,
                )
            self.assertEqual(caught.exception.code, "CLOCK_ROLLBACK")
            after = store.status()
            self.assertEqual(before["state_version"], after["state_version"])
            self.assertEqual(before["round_decisions"], after["round_decisions"])

    def test_sample_generator_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left"
            right = root / "right"
            generate_samples(left)
            generate_samples(right)
            for path in sorted(left.iterdir()):
                self.assertEqual(
                    path.read_bytes(),
                    (right / path.name).read_bytes(),
                    path.name,
                )


if __name__ == "__main__":
    unittest.main()
