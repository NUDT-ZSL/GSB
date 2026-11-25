from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from batchledger.db import LedgerDB
from batchledger.engine import BatchLedgerEngine
from batchledger.fixture import validate_and_build


def _bundle(
    *,
    requests: list[dict],
    compute_capacity: float = 100.0,
    memory_limit_mib: float = 100.0,
    flops: float = 1.0,
    kv_mib_per_token: float = 0.001,
) -> dict:
    return {
        "schema_version": 1,
        "model": {
            "model_id": "constraint-test",
            "layers": [{"name": "layer", "params": 1_000_000_000}],
            "flops_per_parameter_per_token": flops,
            "memory_base_mib": 10.0,
            "kv_mib_per_token": kv_mib_per_token,
            "activation_mib_per_token": 0.0,
            "batch_overhead_ms": 1.0,
        },
        "replicas": [
            {
                "replica_id": "r1",
                "compute_capacity": compute_capacity,
                "memory_limit_mib": memory_limit_mib,
                "window_ms": 100,
                "max_batch_size": 10,
                "max_sequence_length": 1000,
            }
        ],
        "requests": requests,
        "accuracy": {
            "compute_scale": 1024,
            "memory_scale": 1024,
            "latency_scale": 1000,
            "float_tolerance": 0.5,
            "error_budget": 0.1,
            "rounding_mode": "half_up",
        },
        "injections": [],
    }


class ConstraintScenarioTests(unittest.TestCase):
    def _run(self, bundle: dict) -> dict:
        fixture = validate_and_build(bundle)
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "case.db"
            LedgerDB.initialize(db_path, fixture)
            BatchLedgerEngine(db_path).run()
            return json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve().parents[1] / "ledger.py"),
                        "status",
                        "--db",
                        str(db_path),
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout
            )

    def test_compute_budget_is_first_constraint_and_lower_priority_can_pass(self) -> None:
        status = self._run(
            _bundle(
                requests=[
                    {
                        "request_id": 1,
                        "arrival_ms": 1,
                        "priority": 0,
                        "input_tokens": 60,
                        "max_output_tokens": 30,
                        "deadline_ms": 5000,
                    },
                    {
                        "request_id": 2,
                        "arrival_ms": 2,
                        "priority": 1,
                        "input_tokens": 10,
                        "max_output_tokens": 10,
                        "deadline_ms": 5000,
                    },
                    {
                        "request_id": 3,
                        "arrival_ms": 3,
                        "priority": 2,
                        "input_tokens": 3,
                        "max_output_tokens": 2,
                        "deadline_ms": 5000,
                    },
                ]
            )
        )
        skipped = [
            item
            for window in status["windows"]
            for batch in window["batches"]
            for item in batch["decision"]["skipped"]
        ]
        self.assertEqual(skipped[0]["request_id"], 2)
        self.assertEqual(skipped[0]["first_constraint"], "compute_budget")
        accepted = [
            request_id
            for window in status["windows"]
            for batch in window["batches"]
            for request_id in batch["request_ids"]
        ]
        self.assertIn(3, accepted)

    def test_memory_budget_is_first_constraint(self) -> None:
        status = self._run(
            _bundle(
                requests=[
                    {
                        "request_id": 1,
                        "arrival_ms": 1,
                        "priority": 0,
                        "input_tokens": 25,
                        "max_output_tokens": 25,
                        "deadline_ms": 5000,
                    },
                    {
                        "request_id": 2,
                        "arrival_ms": 2,
                        "priority": 1,
                        "input_tokens": 25,
                        "max_output_tokens": 25,
                        "deadline_ms": 5000,
                    },
                    {
                        "request_id": 3,
                        "arrival_ms": 3,
                        "priority": 2,
                        "input_tokens": 10,
                        "max_output_tokens": 10,
                        "deadline_ms": 5000,
                    },
                ],
                compute_capacity=10_000.0,
                memory_limit_mib=100.0,
                kv_mib_per_token=1.0,
            )
        )
        skipped = [
            item
            for window in status["windows"]
            for batch in window["batches"]
            for item in batch["decision"]["skipped"]
        ]
        self.assertEqual(skipped[0]["request_id"], 2)
        self.assertEqual(skipped[0]["first_constraint"], "memory_budget")

    def test_deadline_constraint_records_skip(self) -> None:
        status = self._run(
            _bundle(
                requests=[
                    {
                        "request_id": 1,
                        "arrival_ms": 101,
                        "priority": 0,
                        "input_tokens": 5,
                        "max_output_tokens": 5,
                        "deadline_ms": 400,
                    },
                    {
                        "request_id": 2,
                        "arrival_ms": 102,
                        "priority": 1,
                        "input_tokens": 25,
                        "max_output_tokens": 25,
                        "deadline_ms": 1000,
                    },
                ],
                compute_capacity=100.0,
            )
        )
        skipped = [
            item
            for window in status["windows"]
            for batch in window["batches"]
            for item in batch["decision"]["skipped"]
        ]
        self.assertTrue(skipped)
        self.assertEqual(skipped[0]["request_id"], 2)
        self.assertEqual(skipped[0]["first_constraint"], "deadline")


if __name__ == "__main__":
    unittest.main()
