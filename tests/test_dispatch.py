from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from .helpers import load_sample, service_for


def _plan_snapshot(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        windows = [
            json.loads(row["plan_json"])
            for row in conn.execute(
                "SELECT plan_json FROM windows ORDER BY window_key"
            )
        ]
        batches = []
        for row in conn.execute(
            "SELECT batch_key,request_ids_json,decision_json,estimate_json,"
            "error_spectrum_json,precision_sensitive FROM batches "
            "ORDER BY batch_key"
        ):
            batches.append(
                {
                    "batch_key": row["batch_key"],
                    "request_ids": json.loads(row["request_ids_json"]),
                    "decision": json.loads(row["decision_json"]),
                    "estimate": json.loads(row["estimate_json"]),
                    "error_spectrum": json.loads(row["error_spectrum_json"]),
                    "precision_sensitive": bool(row["precision_sensitive"]),
                }
            )
        return {"windows": windows, "batches": batches}
    finally:
        conn.close()


class DispatchTests(unittest.TestCase):
    def test_sample_plan_and_priority_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(Path(directory))
            load_sample(service)
            result = service.dispatch()
            self.assertTrue(result["ok"])
            self.assertEqual(result["committed_windows"], 10)
            self.assertEqual(result["status"]["committed_batches_total"], 14)
            self.assertEqual(
                result["status"]["request_counts"],
                {"completed": 17, "expired": 1},
            )
            verify = service.verify()
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["stats"]["request_assignments"], 18)
            self.assertGreaterEqual(verify["stats"]["precision_flags"], 1)

            conn = sqlite3.connect(service.db_path)
            conn.row_factory = sqlite3.Row
            try:
                batch = conn.execute(
                    "SELECT * FROM batches WHERE "
                    "batch_key='r0:v1:w000000000000#b0001'"
                ).fetchone()
                self.assertIsNotNone(batch)
                decision = json.loads(batch["decision_json"])
                self.assertEqual(
                    json.loads(batch["request_ids_json"]),
                    ["req-018", "req-017", "req-015"],
                )
                self.assertEqual(decision["first_constraint"], "MAX_BATCH_SIZE")
                self.assertEqual(
                    decision["deferred_requests"],
                    [
                        {
                            "request_seq": 16,
                            "request_id": "req-016",
                            "priority": 20,
                            "reason": "MAX_BATCH_SIZE",
                        }
                    ],
                )
                self.assertEqual(
                    decision["priority_insertions"][0]["request_id"],
                    "req-018",
                )
                self.assertTrue(batch["precision_sensitive"])
                spectrum = json.loads(batch["error_spectrum_json"])
                for quantity_name in (
                    "compute",
                    "memory",
                    "queue_ms",
                    "efficiency",
                ):
                    quantity = spectrum[quantity_name]
                    self.assertEqual(quantity["scale"], 1000)
                    self.assertIn(
                        quantity["rounding_direction"], ("up", "down", "none")
                    )
                    self.assertLessEqual(
                        quantity["accumulated_error_bound"], 0.01
                    )
                    operations = {step["op"] for step in quantity["trace"]}
                    self.assertIn("fixed_convert", operations)
                    self.assertTrue({"add", "multiply"} & operations)
            finally:
                conn.close()

    def test_repeated_runs_are_bitwise_stable_and_dispatch_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = service_for(Path(first_dir), "first")
            second = service_for(Path(second_dir), "second")
            load_sample(first)
            load_sample(second)
            first_result = first.dispatch()
            second_result = second.dispatch()
            self.assertEqual(first_result, second_result)
            self.assertEqual(
                _plan_snapshot(first.db_path),
                _plan_snapshot(second.db_path),
            )
            self.assertEqual(
                first.status(details=True),
                second.status(details=True),
            )
            before = first.status(details=True)
            again = first.dispatch()
            self.assertEqual(again["committed_windows"], 0)
            self.assertEqual(again["prepared_windows"], 0)
            after = first.status(details=True)
            for key in (
                "committed_windows_total",
                "committed_batches_total",
                "precision_sensitive_total",
                "request_counts",
                "outcome_counts",
            ):
                self.assertEqual(before[key], after[key])
            self.assertTrue(first.verify()["ok"])

    def test_repeated_window_submission_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(Path(directory))
            load_sample(service)
            service.dispatch()
            conn = sqlite3.connect(service.db_path)
            conn.row_factory = sqlite3.Row
            try:
                plan = json.loads(
                    conn.execute(
                        "SELECT plan_json FROM windows ORDER BY window_key LIMIT 1"
                    ).fetchone()["plan_json"]
                )
            finally:
                conn.close()
            before = service.status(details=False)
            self.assertEqual(service.store.prepare_window(plan), "committed")
            self.assertEqual(
                service.store.commit_window(plan), "already_committed"
            )
            after = service.status(details=False)
            for key in (
                "committed_windows_total",
                "committed_batches_total",
                "request_counts",
            ):
                self.assertEqual(before[key], after[key])
            self.assertTrue(service.verify()["ok"])


if __name__ == "__main__":
    unittest.main()

