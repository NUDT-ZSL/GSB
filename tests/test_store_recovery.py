from __future__ import annotations

import json
import unittest

from gateway.store import Store
from gateway.verify import verify_store
from tests.helpers import (
    TempDirTestCaseMixin,
    config_doc,
    load_cli,
    request,
    request_doc,
    run_cli,
    service_doc,
    write_json,
)


class StoreRecoveryTests(TempDirTestCaseMixin, unittest.TestCase):
    def _load_single(self, db) -> None:
        result = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_precommit_crash_rolls_back_then_replays_once(self) -> None:
        for failpoint in (
            "after_mutation",
            "after_snapshot_insert",
            "before_commit",
        ):
            with self.subTest(failpoint=failpoint):
                temp = self.make_temp_dir()
                db = temp / f"{failpoint}.db"
                self._load_single(db)
                crashed = run_cli(
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "1",
                    "--round-id",
                    "r1",
                    env={"GW_FAILPOINT": failpoint},
                )
                self.assertEqual(crashed.returncode, 137, crashed.stderr)
                state, version, _ = Store(db).read_state()
                self.assertEqual(version, 1)
                self.assertEqual(state["counters"]["dispatches"], 0)
                self.assertEqual(state["requests"]["1"]["generation"], 0)
                self.assertEqual(
                    state["global_token"]["balance_fixed"],
                    state["global_token"]["capacity_fixed"],
                )

                rerun = run_cli(
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "1",
                    "--round-id",
                    "r1",
                )
                self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                state, version, _ = Store(db).read_state()
                self.assertEqual(version, 2)
                self.assertEqual(state["counters"]["dispatches"], 1)
                self.assertEqual(state["requests"]["1"]["generation"], 1)
                self.assertEqual(
                    state["global_token"]["balance_fixed"],
                    state["global_token"]["capacity_fixed"] - 1000,
                )
                self.assertTrue(verify_store(Store(db))["verified"])

    def test_postcommit_crash_is_detected_and_resumed_without_double_charge(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        self._load_single(db)
        crashed = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "1",
            "--round-id",
            "r1",
            env={"GW_FAILPOINT": "after_commit"},
        )
        self.assertEqual(crashed.returncode, 137)
        state, version, _ = Store(db).read_state()
        self.assertEqual(version, 2)
        self.assertEqual(state["counters"]["dispatches"], 1)
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertEqual(
            state["requests"]["1"]["attempts"][0]["status"], "inflight"
        )

        recovered = run_cli(
            "resume",
            "--db",
            str(db),
            "--clock-ms",
            "10",
            "--round-id",
            "recover-1",
            "--abort-overdue",
            "--recover-only",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        payload = json.loads(recovered.stdout)
        self.assertEqual(
            payload["round"]["recovery"]["inflight_detected"][0]["generation"], 1
        )
        self.assertEqual(len(payload["round"]["recovery"]["expired"]), 1)
        state, version, _ = Store(db).read_state()
        self.assertEqual(version, 3)
        self.assertEqual(state["counters"]["dispatches"], 1)
        self.assertEqual(state["counters"]["expirations"], 1)
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertEqual(state["requests"]["1"]["status"], "retry_wait")
        self.assertEqual(state["global_token"]["total_spent_fixed"], 1000)

        resumed = run_cli(
            "resume",
            "--db",
            str(db),
            "--clock-ms",
            "20",
            "--round-id",
            "recover-2",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        state, version, _ = Store(db).read_state()
        self.assertEqual(state["requests"]["1"]["generation"], 2)
        self.assertEqual(state["counters"]["dispatches"], 2)
        self.assertEqual(state["global_token"]["total_spent_fixed"], 2000)

        duplicate = run_cli(
            "resume",
            "--db",
            str(db),
            "--clock-ms",
            "20",
            "--round-id",
            "recover-2",
        )
        self.assertEqual(duplicate.returncode, 0)
        state_after, version_after, _ = Store(db).read_state()
        self.assertEqual(version_after, version)
        self.assertEqual(state_after["counters"]["dispatches"], 2)
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_confirmation_crash_before_commit_is_idempotent(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        self._load_single(db)
        dispatched = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "1",
            "--round-id",
            "r1",
        )
        self.assertEqual(dispatched.returncode, 0)
        outcomes = write_json(
            temp / "outcomes.json",
            [
                {
                    "request_id": 1,
                    "generation": 1,
                    "replica_id": "r1",
                    "outcome": "success",
                }
            ],
        )
        crashed = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "2",
            "--round-id",
            "confirm-1",
            "--outcomes",
            str(outcomes),
            "--no-dispatch",
            env={"GW_FAILPOINT": "before_commit"},
        )
        self.assertEqual(crashed.returncode, 137)
        state, version, _ = Store(db).read_state()
        self.assertEqual(version, 2)
        self.assertFalse(state["requests"]["1"]["confirmed"])
        self.assertEqual(
            state["requests"]["1"]["attempts"][0]["status"], "inflight"
        )
        self.assertEqual(state["counters"]["successes"], 0)

        rerun = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "2",
            "--round-id",
            "confirm-1",
            "--outcomes",
            str(outcomes),
            "--no-dispatch",
        )
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        state, version, _ = Store(db).read_state()
        self.assertEqual(version, 3)
        self.assertTrue(state["requests"]["1"]["confirmed"])
        self.assertEqual(state["counters"]["successes"], 1)
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_round_id_conflict(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        self._load_single(db)
        first = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "1",
            "--round-id",
            "same-id",
        )
        self.assertEqual(first.returncode, 0)
        version_before = Store(db).read_state()[1]
        conflict = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "2",
            "--round-id",
            "same-id",
        )
        self.assertEqual(conflict.returncode, 2)
        payload = json.loads(conflict.stdout)
        self.assertEqual(payload["error"]["code"], "E_IDEMPOTENCY_CONFLICT")
        self.assertEqual(Store(db).read_state()[1], version_before)


if __name__ == "__main__":
    unittest.main()
