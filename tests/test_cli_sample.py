from __future__ import annotations

import json
import unittest
from pathlib import Path

from gateway.store import Store
from gateway.verify import verify_store
from tests.helpers import (
    ROOT,
    TempDirTestCaseMixin,
    config_doc,
    load_cli,
    request,
    request_doc,
    run_cli,
    service_doc,
    write_json,
)


SAMPLE_SERVICES = ROOT / "samples" / "services.json"
SAMPLE_REQUESTS = ROOT / "samples" / "requests.json"
SAMPLE_CONFIG = ROOT / "samples" / "config.json"
SAMPLE_STEPS = ROOT / "samples" / "clock_steps.json"


class CliSampleTests(TempDirTestCaseMixin, unittest.TestCase):
    def _load_sample(self, db: Path) -> None:
        result = run_cli(
            "load",
            "--db",
            str(db),
            "--services",
            str(SAMPLE_SERVICES),
            "--requests",
            str(SAMPLE_REQUESTS),
            "--config",
            str(SAMPLE_CONFIG),
            "--clock-steps",
            str(SAMPLE_STEPS),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sample_plan_budget_recovery_and_verify(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "sample.db"
        self._load_sample(db)
        dispatched = run_cli(
            "dispatch", "--db", str(db), "--plan", str(SAMPLE_STEPS)
        )
        self.assertEqual(
            dispatched.returncode, 0, dispatched.stdout + dispatched.stderr
        )
        payload = json.loads(dispatched.stdout)
        rounds = {item["round_id"]: item for item in payload["rounds"]}
        budget_round = rounds["s2-dispatch-alpha-budget"]
        self.assertTrue(budget_round["budget_exhausted"])
        self.assertTrue(
            any(
                item.get("reason") == "budget_exhausted"
                for item in budget_round["not_started"]
            )
        )
        recovery_round = rounds["s5-recover-overdue"]
        self.assertEqual(recovery_round["recovery"]["expired"][0]["request_id"], 4)
        self.assertTrue(
            any(
                item["request_id"] == 4
                for item in recovery_round["failed_after_start"]
            )
        )

        verified = run_cli("verify", "--db", str(db))
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        verify_payload = json.loads(verified.stdout)
        self.assertTrue(verify_payload["verified"])

        status = run_cli("status", "--db", str(db))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        status_json = json.loads(status.stdout)
        self.assertEqual(
            status_json["logical_clock_watermark_ms"],
            60,
        )
        self.assertEqual(status_json["global_token"]["scale"], 1000)
        self.assertEqual(
            status_json["requests"]["1"]["status"],
            "confirmed",
        )
        self.assertEqual(
            status_json["requests"]["2"]["generation"],
            2,
        )
        self.assertEqual(
            status_json["requests"]["4"]["status"],
            "retry_wait",
        )
        self.assertGreaterEqual(
            status_json["services"]["alpha"]["replicas"][0]["used_slots"], 0
        )

        version_before = Store(db).read_state()[1]
        repeated = run_cli(
            "dispatch", "--db", str(db), "--plan", str(SAMPLE_STEPS)
        )
        self.assertEqual(repeated.returncode, 0)
        repeated_payload = json.loads(repeated.stdout)
        self.assertTrue(all(item["idempotent"] for item in repeated_payload["rounds"]))
        self.assertEqual(Store(db).read_state()[1], version_before)
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_sample_is_bit_identical_across_fresh_databases(self) -> None:
        hashes = []
        for index in range(2):
            temp = self.make_temp_dir()
            db = temp / f"sample-{index}.db"
            self._load_sample(db)
            result = run_cli(
                "dispatch", "--db", str(db), "--plan", str(SAMPLE_STEPS)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = verify_store(Store(db))
            self.assertTrue(report["verified"])
            hashes.append(report["state_hash"])
        self.assertEqual(hashes[0], hashes[1])

    def test_status_is_stable_sorted_json(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "sample.db"
        self._load_sample(db)
        first = run_cli("status", "--db", str(db))
        second = run_cli("status", "--db", str(db))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(list(payload["services"]), ["alpha", "beta"])
        self.assertEqual(list(payload["requests"]), ["1", "2", "3", "4", "5", "6"])

    def test_error_codes_and_diagnostics(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        services = service_doc(
            replicas=[{"replica_id": "r1", "slots": 1, "compute_units": 1}]
        )
        loaded = load_cli(
            db,
            services,
            request_doc([request(1), request(2, priority=2)]),
            config_doc(tolerance_ms=1),
        )
        self.assertEqual(loaded.returncode, 0)

        missing_target = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "1",
            "--round-id",
            "missing-target",
            "--force-target",
            "1=missing",
        )
        self.assertEqual(missing_target.returncode, 0)
        missing_payload = json.loads(missing_target.stdout)
        missing_entry = next(
            item
            for item in missing_payload["round"]["not_started"]
            if item["request_id"] == 1
        )
        self.assertEqual(missing_entry["error_code"], "E_REPLICA_NOT_FOUND")
        self.assertEqual(missing_entry["replica_id"], "missing")

        full_db = temp / "full.db"
        full_loaded = load_cli(
            full_db,
            services,
            request_doc([request(1), request(2, priority=2)]),
            config_doc(tolerance_ms=1),
        )
        self.assertEqual(full_loaded.returncode, 0)
        full_targets = run_cli(
            "dispatch",
            "--db",
            str(full_db),
            "--clock-ms",
            "1",
            "--round-id",
            "full-targets",
            "--force-target",
            "1=r1",
            "--force-target",
            "2=r1",
            "--budget-ms",
            "100",
        )
        self.assertEqual(full_targets.returncode, 0)
        full_payload = json.loads(full_targets.stdout)
        started_ids = {item["request_id"] for item in full_payload["round"]["started"]}
        full_entries = [
            item
            for item in full_payload["round"]["not_started"]
            if item.get("error_code") == "E_REPLICA_SLOTS_FULL"
        ]
        self.assertEqual(len(started_ids), 1)
        self.assertEqual(len(full_entries), 1)
        self.assertIn("request_id", full_entries[0])
        self.assertIn("generation", full_entries[0])

        version_before = Store(db).read_state()[1]
        outcomes = write_json(
            temp / "missing-outcome.json",
            [
                {
                    "request_id": 1,
                    "generation": 99,
                    "replica_id": "r1",
                    "outcome": "success",
                }
            ],
        )
        not_found = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "2",
            "--round-id",
            "missing-outcome",
            "--outcomes",
            str(outcomes),
            "--no-dispatch",
        )
        self.assertEqual(not_found.returncode, 2)
        not_found_payload = json.loads(not_found.stdout)
        self.assertEqual(
            not_found_payload["error"]["code"], "E_CONFIRM_NOT_FOUND"
        )
        self.assertEqual(not_found_payload["error"]["request_id"], 1)
        self.assertEqual(Store(db).read_state()[1], version_before)

        advanced = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "2",
            "--round-id",
            "advance-clock",
            "--no-dispatch",
        )
        self.assertEqual(advanced.returncode, 0)
        clock_regression = run_cli(
            "dispatch",
            "--db",
            str(db),
            "--clock-ms",
            "0",
            "--round-id",
            "clock-regression",
        )
        self.assertEqual(clock_regression.returncode, 2)
        clock_payload = json.loads(clock_regression.stdout)
        self.assertEqual(
            clock_payload["error"]["code"], "E_CLOCK_REGRESSION"
        )
        self.assertEqual(
            clock_payload["error"]["clock_watermark_ms"], 2
        )


if __name__ == "__main__":
    unittest.main()
