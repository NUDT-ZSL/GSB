from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elastic_control.engine import apply_patch, load_workspace
from elastic_control.errors import DEPENDENCY_CYCLE, DUPLICATE_NAME, MISSING_REFERENCE, ControlError
from elastic_control.models import PolicyBundle
from elastic_control.samplegen import generate_samples


def valid_payload() -> dict:
    return {
        "workloads": [
            {
                "name": "api",
                "current_replicas": 2,
                "min_replicas": 1,
                "max_replicas": 5,
                "cpu_millicores": 250,
                "memory_mib": 128,
            }
        ],
        "metrics": [
            {
                "name": "cpu",
                "aggregation": "average",
                "window_ms": 1000,
                "target_value": 0.6,
                "alert_threshold": 0.8,
                "scale": 1000,
                "error_budget": 0.01,
            }
        ],
        "rules": [
            {
                "name": "up",
                "workload": "api",
                "metric": "cpu",
                "direction": "up",
                "step": 1,
                "percent": 10,
                "priority": 10,
                "cooldown_ms": 1000,
                "freeze_ms": 1000,
            }
        ],
        "budget": {
            "pool_cpu_core_ms": 1000,
            "pool_memory_mib": 1024,
            "round_time_budget_ms": 1000,
            "per_workload_concurrency": 1,
            "float_tolerance": 0.01,
            "error_upper_bound": 1,
        },
    }


class ValidationTests(unittest.TestCase):
    def test_duplicate_names_are_rejected(self) -> None:
        payload = valid_payload()
        payload["workloads"].append(dict(payload["workloads"][0]))
        with self.assertRaises(ControlError) as caught:
            PolicyBundle.from_payload(payload)
        self.assertEqual(caught.exception.code, DUPLICATE_NAME)

    def test_missing_reference_is_rejected(self) -> None:
        payload = valid_payload()
        payload["rules"][0]["metric"] = "missing"
        with self.assertRaises(ControlError) as caught:
            PolicyBundle.from_payload(payload)
        self.assertEqual(caught.exception.code, MISSING_REFERENCE)

    def test_invalid_load_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generate_samples(data)
            workloads_path = data / "workloads.json"
            workloads = json.loads(workloads_path.read_text(encoding="utf-8"))
            workloads["workloads"].append(dict(workloads["workloads"][0]))
            workloads_path.write_text(json.dumps(workloads), encoding="utf-8")
            database = root / "control.db"
            with self.assertRaises(ControlError) as caught:
                load_workspace(
                    data_dir=data,
                    db_path=database,
                    reset=True,
                )
            self.assertEqual(caught.exception.code, DUPLICATE_NAME)
            self.assertFalse(database.exists())

    def test_invalid_patch_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generate_samples(data)
            database = root / "control.db"
            load_workspace(data_dir=data, db_path=database, reset=True)
            from elastic_control.store import Store

            store = Store(database)
            before = store.status()
            bad_patch = {
                "base_version": 1,
                "operations": [
                    {"op": "delete", "kind": "metric", "name": "api_cpu"}
                ],
            }
            with self.assertRaises(ControlError) as caught:
                apply_patch(store, bad_patch)
            self.assertEqual(caught.exception.code, MISSING_REFERENCE)
            after = store.status()
            self.assertEqual(before["state_version"], after["state_version"])
            self.assertEqual(before["policy_version"], after["policy_version"])
            self.assertEqual(before["metric_ledgers"], after["metric_ledgers"])


if __name__ == "__main__":
    unittest.main()
