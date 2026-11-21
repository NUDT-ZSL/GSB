from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from .helpers import SAMPLES, cli, load_sample, parse_json, status


class SchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tempdir.name) / "state"
        load_sample(self.state)
        cli(self.state, "schedule", "--clock", "1200", check=True)
        self.state_payload = status(self.state)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _windows(self, replica_id: str) -> list[dict]:
        replica = next(
            item
            for item in self.state_payload["replicas"]
            if item["replica_id"] == replica_id
        )
        return replica["windows"]

    def test_window_ownership_priority_and_precision_sensitivity(self) -> None:
        r0 = self._windows("r0")
        self.assertEqual(
            [(w["window_index"], w["request_seqs"]) for w in r0 if w["request_seqs"]],
            [(0, [2, 4, 6]), (1, [8])],
        )
        batch0 = r0[0]["batches"][0]
        self.assertEqual(batch0["request_seqs"], [4, 2])
        self.assertTrue(batch0["precision_sensitive"])
        self.assertEqual(
            [(item["request_seq"], item["first_trigger"]) for item in batch0["skipped"]],
            [(6, "compute")],
        )
        sensitive = batch0["precision_sensitive_decisions"][0]
        self.assertEqual(sensitive["metric"], "compute")
        self.assertEqual(sensitive["absolute_gap"], 0.0)
        self.assertTrue(sensitive["source_chain"])
        alternative_decisions = [
            item
            for item in batch0["precision_sensitive_decisions"]
            if item["decision"] == "candidate_batch_alternative"
        ]
        self.assertTrue(alternative_decisions)
        self.assertEqual(
            alternative_decisions[0]["right"]["request_seqs"], [4, 6]
        )

        r1 = self._windows("r1")
        self.assertEqual(r1[0]["request_seqs"], [1, 3, 5])
        self.assertEqual(r1[0]["expired_request_seqs"], [7])
        self.assertEqual(r1[0]["batches"][0]["request_seqs"], [3, 5, 1])
        self.assertTrue(r1[0]["batches"][0]["precision_sensitive"])

    def test_fixed_point_and_error_spectrum(self) -> None:
        batch = self._windows("r0")[0]["batches"][0]
        estimates = {item["metric"]: item for item in batch["estimates"]}
        self.assertEqual(
            estimates["compute"]["raw_value"],
            200.0,
        )
        self.assertEqual(estimates["compute"]["fixed_value"], 200000)
        self.assertEqual(estimates["compute"]["scale"], 1000)
        self.assertEqual(estimates["compute"]["rounding_direction"], "exact")
        self.assertEqual(estimates["queue"]["raw_value"], 1000.0)
        self.assertLessEqual(estimates["compute"]["total_error_bound"], 0.01)
        self.assertTrue(
            any(
                operation["kind"] == "multiply"
                for operation in estimates["compute"]["operation_order"]
            )
        )

    def test_lifecycle_expiry_failure_and_counters(self) -> None:
        requests = {
            item["request_seq"]: item for item in self.state_payload["requests"]
        }
        self.assertEqual(requests[7]["state"], "expired")
        self.assertEqual(requests[7]["failure_count"], 0)
        self.assertEqual(requests[7]["attempt_count"], 0)
        self.assertTrue(
            any(
                event["event_type"] == "REQUEST_EXPIRED"
                and event["failure_count_after"] == 0
                for event in requests[7]["lifecycle_events"]
            )
        )
        self.assertEqual(requests[2]["state"], "failed")
        self.assertEqual(requests[2]["failure_count"], 1)
        self.assertEqual(requests[2]["attempt_count"], 1)
        failure_event = next(
            event
            for event in requests[2]["lifecycle_events"]
            if event["event_type"] == "REQUEST_EXECUTION_FAILED"
        )
        self.assertEqual(failure_event["failure_count_before"], 0)
        self.assertEqual(failure_event["failure_count_after"], 1)
        self.assertEqual(self.state_payload["request_state_counters"]["expired"], 1)
        self.assertEqual(self.state_payload["request_state_counters"]["failed"], 1)

    def test_status_is_byte_stable_and_verify_passes(self) -> None:
        first = cli(self.state, "status")
        second = cli(self.state, "status")
        self.assertEqual(first.stdout, second.stdout)
        verify_result = cli(self.state, "verify", check=True)
        self.assertTrue(parse_json(verify_result)["ok"])

    def test_request_reload_after_execution_is_idempotent(self) -> None:
        before = status(self.state)
        result = cli(
            self.state,
            "load",
            "--requests",
            str(SAMPLES / "requests.json"),
            check=True,
        )
        payload = parse_json(result)["result"]
        self.assertEqual(payload["requests_inserted"], 0)
        self.assertEqual(payload["requests_unchanged"], 8)
        self.assertEqual(status(self.state), before)

    def test_consecutive_failure_count_and_retry_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            requests = {
                "requests": [
                    {
                        "request_seq": 1,
                        "arrival_ms": 0,
                        "priority": 1,
                        "input_tokens": 6,
                        "max_output_tokens": 4,
                        "deadline_ms": 5000,
                        "simulate_failures": 3,
                    }
                ]
            }
            request_path = Path(temp) / "requests.json"
            request_path.write_text(json.dumps(requests), encoding="utf-8")
            cli(
                state,
                "load",
                "--model",
                str(SAMPLES / "model.json"),
                "--replicas",
                str(SAMPLES / "replicas.json"),
                "--requests",
                str(request_path),
                "--precision",
                str(SAMPLES / "precision.json"),
                "--reset",
                check=True,
            )
            cli(state, "schedule", "--clock", "1200", check=True)
            request = status(state)["requests"][0]
            self.assertEqual(request["state"], "failed")
            self.assertEqual(request["failure_count"], 3)
            self.assertEqual(request["attempt_count"], 3)
            connection = sqlite3.connect(state / "scheduler.sqlite3")
            try:
                event_types = [
                    row[0]
                    for row in connection.execute(
                        "SELECT event_type FROM ledger "
                        "WHERE request_seq = 1 ORDER BY event_id"
                    ).fetchall()
                ]
            finally:
                connection.close()
            self.assertEqual(
                event_types.count("REQUEST_EXECUTION_FAILED"), 3
            )
            self.assertEqual(event_types.count("REQUEST_RETRY_EXECUTING"), 2)

    def test_error_budget_exceeded_is_traced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            precision = json.loads(
                (SAMPLES / "precision.json").read_text(encoding="utf-8")
            )
            precision["error_bound_budget"] = 1e-12
            precision_path = Path(temp) / "precision.json"
            precision_path.write_text(json.dumps(precision), encoding="utf-8")
            cli(
                state,
                "load",
                "--model",
                str(SAMPLES / "model.json"),
                "--replicas",
                str(SAMPLES / "replicas.json"),
                "--requests",
                str(SAMPLES / "requests.json"),
                "--precision",
                str(precision_path),
                "--reset",
                check=True,
            )
            cli(state, "schedule", "--clock", "200", check=True)
            payload = status(state)
            decisions = [
                decision
                for replica in payload["replicas"]
                for window in replica["windows"]
                for batch in window["batches"]
                if batch["precision_sensitive"]
                for decision in batch["precision_sensitive_decisions"]
                if decision["decision"] == "error_budget_exceeded"
            ]
            self.assertTrue(decisions)
            self.assertTrue(all(decision["source_chain"] for decision in decisions))
            verification = parse_json(cli(state, "verify", check=True))
            self.assertTrue(verification["ok"])
            self.assertEqual(verification["verification"]["summary"]["warnings"], 1)

    def test_chunked_clock_advance_matches_single_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            chunked_state = Path(temp) / "state"
            load_sample(chunked_state)
            cli(chunked_state, "schedule", "--clock", "500", check=True)
            cli(chunked_state, "schedule", "--clock", "1200", check=True)
            self.assertEqual(status(chunked_state), self.state_payload)

    def test_clock_regression_is_rejected_without_state_change(self) -> None:
        before = status(self.state)
        result = cli(self.state, "schedule", "--clock", "1199")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(parse_json(result)["error"]["code"], "E_CLOCK_REGRESSION")
        self.assertEqual(status(self.state), before)

    def test_replica_version_actions_and_window_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            version_state = Path(temp) / "state"
            load_sample(version_state)
            cli(version_state, "schedule", "--clock", "100", check=True)
            before = status(version_state)
            old_r0_windows = [
                window
                for replica in before["replicas"]
                if replica["replica_id"] == "r0"
                for window in replica["windows"]
            ]
            self.assertTrue(all(window["replica_version"] == 1 for window in old_r0_windows))

            appended = cli(
                version_state,
                "load",
                "--replicas",
                str(SAMPLES / "replicas_v2_appended.json"),
                check=True,
            )
            appended_result = parse_json(appended)["result"]
            self.assertEqual(
                appended_result["replica_change"]["action"], "incremental_append"
            )
            self.assertEqual(appended_result["replica_version"], 2)

            no_op = cli(
                version_state,
                "load",
                "--replicas",
                str(SAMPLES / "replicas_v2_appended.json"),
                check=True,
            )
            self.assertEqual(
                parse_json(no_op)["result"]["replica_change"]["action"], "no_op"
            )

            rollback = cli(
                version_state,
                "load",
                "--replicas",
                str(SAMPLES / "replicas.json"),
            )
            self.assertEqual(rollback.returncode, 2)
            self.assertEqual(
                parse_json(rollback)["error"]["code"],
                "E_REPLICA_VERSION_ROLLBACK",
            )

            replaced = cli(
                version_state,
                "load",
                "--replicas",
                str(SAMPLES / "replicas_v3_replaced.json"),
                check=True,
            )
            self.assertEqual(
                parse_json(replaced)["result"]["replica_change"]["action"],
                "full_replace",
            )

            cli(version_state, "schedule", "--clock", "300", check=True)
            after = status(version_state)
            versions = [
                window["replica_version"]
                for replica in after["replicas"]
                if replica["replica_id"] == "r0"
                for window in replica["windows"]
            ]
            self.assertIn(1, versions)
            self.assertIn(3, versions)


if __name__ == "__main__":
    unittest.main()

