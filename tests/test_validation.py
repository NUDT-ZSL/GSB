from __future__ import annotations

import tempfile
import unittest
import copy
import json
from pathlib import Path

from schedkernel.errors import KernelError
from schedkernel.validation import validate_dataset

from .helpers import SAMPLES, load_sample, service_for


class ValidationTests(unittest.TestCase):
    def test_duplicate_request_is_rejected_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            service = service_for(tmp)
            with self.assertRaises(KernelError) as raised:
                service.load_files(
                    SAMPLES / "model.json",
                    SAMPLES / "replicas.json",
                    SAMPLES / "invalid-duplicate.json",
                    SAMPLES / "precision.json",
                )
            self.assertEqual(raised.exception.code, "E_DUP_REQ")
            self.assertFalse(service.db_path.exists())

    def test_request_without_replica_is_rejected_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            service = service_for(tmp)
            with self.assertRaises(KernelError) as raised:
                service.load_files(
                    SAMPLES / "model.json",
                    SAMPLES / "replicas.json",
                    SAMPLES / "invalid-no-replica.json",
                    SAMPLES / "precision.json",
                )
            self.assertEqual(raised.exception.code, "E_REQ_NO_REPLICA")
            self.assertFalse(service.db_path.exists())

    def test_incremental_version_and_rollback_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            service = service_for(tmp)
            first = load_sample(service)
            self.assertEqual(first["change_kind"], "initial")
            second = load_sample(service, "replicas-v2.json")
            self.assertEqual(second["change_kind"], "incremental_append")
            self.assertEqual(second["active_replica_version"], 2)
            with self.assertRaises(KernelError) as raised:
                load_sample(service)
            self.assertEqual(raised.exception.code, "E_VERSION_ROLLBACK")
            status = service.status()
            self.assertEqual(status["active_replica_version"], 2)

    def test_noop_full_replace_and_invalid_boundaries(self) -> None:
        model = json.loads((SAMPLES / "model.json").read_text())
        replicas = json.loads((SAMPLES / "replicas.json").read_text())
        requests = json.loads((SAMPLES / "requests.json").read_text())
        precision = json.loads((SAMPLES / "precision.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            service = service_for(Path(directory))
            dataset = validate_dataset(model, replicas, requests, precision)
            self.assertEqual(service.load_dataset(dataset)["change_kind"], "initial")
            self.assertEqual(service.load_dataset(dataset)["change_kind"], "no_op")
            changed = copy.deepcopy(replicas)
            changed["version"] = 2
            changed["replicas"][0]["available_compute_units"] = 161.0
            full = validate_dataset(model, changed, requests, precision)
            loaded = service.load_dataset(full)
            self.assertEqual(loaded["change_kind"], "full_replace")
            self.assertEqual(loaded["active_replica_version"], 2)

        bad_requests = copy.deepcopy(requests)
        bad_requests["requests"][0]["arrival_ms"] = -1
        with self.assertRaises(KernelError) as bad_arrival:
            validate_dataset(model, replicas, bad_requests, precision)
        self.assertEqual(bad_arrival.exception.code, "E_BAD_REQ")

        bad_deadline = copy.deepcopy(requests)
        bad_deadline["requests"][0]["deadline_ms"] = (
            bad_deadline["requests"][0]["arrival_ms"] - 1
        )
        with self.assertRaises(KernelError) as bad_deadline_error:
            validate_dataset(model, replicas, bad_deadline, precision)
        self.assertEqual(bad_deadline_error.exception.code, "E_BAD_REQ")

        bad_replicas = copy.deepcopy(replicas)
        bad_replicas["replicas"][1]["replica_id"] = "r0"
        with self.assertRaises(KernelError) as duplicate_replica:
            validate_dataset(model, bad_replicas, requests, precision)
        self.assertEqual(duplicate_replica.exception.code, "E_DUP_REPLICA")

        zero_compute = copy.deepcopy(replicas)
        zero_compute["replicas"][0]["available_compute_units"] = 0
        with self.assertRaises(KernelError) as bad_compute:
            validate_dataset(model, zero_compute, requests, precision)
        self.assertEqual(bad_compute.exception.code, "E_BAD_REPLICA")


if __name__ == "__main__":
    unittest.main()

