from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from schedkernel.validation import validate_dataset

from .helpers import SAMPLES, service_for


def _one_batch_decision(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT decision_json FROM batches ORDER BY batch_key LIMIT 1"
        ).fetchone()
        return json.loads(row["decision_json"])
    finally:
        conn.close()


class BudgetConstraintTests(unittest.TestCase):
    def test_compute_budget_is_recorded_as_first_constraint(self) -> None:
        model = json.loads((SAMPLES / "model.json").read_text())
        precision = json.loads((SAMPLES / "precision.json").read_text())
        replicas = {
            "version": 1,
            "replicas": [
                {
                    "replica_id": "only",
                    "available_compute_units": 60.0,
                    "memory_limit_mib": 96,
                    "batch_window_ms": 10,
                    "max_batch_size": 10,
                    "max_sequence_length": 512,
                }
            ],
        }
        requests = {
            "requests": [
                {
                    "request_seq": 1,
                    "request_id": "a",
                    "arrival_ms": 0,
                    "priority": 10,
                    "input_tokens": 200,
                    "max_output_tokens": 100,
                    "deadline_ms": 50,
                },
                {
                    "request_seq": 2,
                    "request_id": "b",
                    "arrival_ms": 1,
                    "priority": 5,
                    "input_tokens": 200,
                    "max_output_tokens": 100,
                    "deadline_ms": 50,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(Path(directory))
            service.load_dataset(
                validate_dataset(model, replicas, requests, precision)
            )
            service.dispatch()
            decision = _one_batch_decision(service.db_path)
            self.assertEqual(decision["first_constraint"], "COMPUTE_BUDGET")
            self.assertEqual(
                decision["deferred_requests"][0]["reason"],
                "COMPUTE_BUDGET",
            )
            self.assertTrue(service.verify()["ok"])

    def test_memory_budget_is_recorded_as_first_constraint(self) -> None:
        model = json.loads((SAMPLES / "model.json").read_text())
        model["memory"]["activation_base_mib"] = 2.0
        model["memory"]["request_base_mib"] = 1.5
        precision = json.loads((SAMPLES / "precision.json").read_text())
        replicas = {
            "version": 1,
            "replicas": [
                {
                    "replica_id": "only",
                    "available_compute_units": 1000.0,
                    "memory_limit_mib": 11,
                    "batch_window_ms": 10,
                    "max_batch_size": 10,
                    "max_sequence_length": 512,
                }
            ],
        }
        requests = {
            "requests": [
                {
                    "request_seq": 1,
                    "request_id": "a",
                    "arrival_ms": 0,
                    "priority": 10,
                    "input_tokens": 64,
                    "max_output_tokens": 32,
                    "deadline_ms": 50,
                },
                {
                    "request_seq": 2,
                    "request_id": "b",
                    "arrival_ms": 1,
                    "priority": 5,
                    "input_tokens": 64,
                    "max_output_tokens": 32,
                    "deadline_ms": 50,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(Path(directory))
            service.load_dataset(
                validate_dataset(model, replicas, requests, precision)
            )
            service.dispatch()
            decision = _one_batch_decision(service.db_path)
            self.assertEqual(decision["first_constraint"], "MEMORY_BUDGET")
            self.assertEqual(
                decision["deferred_requests"][0]["reason"],
                "MEMORY_BUDGET",
            )
            self.assertTrue(service.verify()["ok"])


if __name__ == "__main__":
    unittest.main()

